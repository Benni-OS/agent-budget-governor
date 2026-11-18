"""Facade orchestrating pricing, hierarchical caps, circuit breaker, and SHA-256 ledger."""

from __future__ import annotations

import itertools
import threading
import time
from typing import TYPE_CHECKING, Any

from agent_budget_governor.core.circuit_breaker import EconomicCircuitBreaker
from agent_budget_governor.core.ledger import SpendLedger
from agent_budget_governor.core.limiter import HierarchicalLimiter
from agent_budget_governor.core.models import (
    BudgetPolicy,
    BudgetReceipt,
    BudgetVerdict,
    CircuitState,
    SpendEvent,
)
from agent_budget_governor.core.pricing import PricingCatalog

if TYPE_CHECKING:
    from agent_budget_governor.serve.telemetry import BudgetTelemetryEngine


class BudgetGovernorEngine:
    """Zero-cloud FinOps governor enforcing 0% budget overrun across AI agent swarms."""

    def __init__(
        self,
        policy: BudgetPolicy | None = None,
        catalog: PricingCatalog | None = None,
        telemetry: BudgetTelemetryEngine | None = None,
    ) -> None:
        self.policy = policy or BudgetPolicy()
        self.catalog = catalog or PricingCatalog()
        self.limiter = HierarchicalLimiter(policy=self.policy)
        self.circuit_breaker = EconomicCircuitBreaker(
            policy=self.policy,
            catalog=self.catalog,
        )
        self.ledger = SpendLedger()
        self.telemetry = telemetry
        self._lock = threading.RLock()
        self._verdict_seq = itertools.count(1)
        self._event_seq = itertools.count(1)

    def authorize_call(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        run_id: str,
        step_id: str,
        model: str,
        estimated_input_tokens: int = 0,
        estimated_cached_tokens: int = 0,
        estimated_output_tokens: int = 0,
        estimated_reasoning_tokens: int = 0,
        estimated_tool_calls: int = 0,
        timestamp: float | None = None,
    ) -> BudgetVerdict:
        """Evaluate pre-flight authorization, auto-downgrade routing, and overrun guards."""
        t0 = time.perf_counter()
        now = timestamp if timestamp is not None else time.time()
        verdict_id = f"BV-{next(self._verdict_seq):06d}"

        with self._lock:
            requested_breakdown = self.catalog.calculate_cost(
                model,
                input_tokens=estimated_input_tokens,
                cached_input_tokens=estimated_cached_tokens,
                output_tokens=estimated_output_tokens,
                reasoning_tokens=estimated_reasoning_tokens,
                tool_calls=estimated_tool_calls,
            )
            est_tokens = requested_breakdown.total_tokens

            # Probe current envelope with requested model cost
            env_req = self.limiter.check_envelope(
                tenant_id=tenant_id,
                agent_id=agent_id,
                run_id=run_id,
                step_id=step_id,
                estimated_cost_usd=requested_breakdown.total_cost_usd,
                estimated_tokens=est_tokens,
                timestamp=now,
            )

            circuit = self.circuit_breaker.evaluate(
                tenant_id=tenant_id,
                requested_model=model,
                utilization_ratio=env_req.current_utilization_ratio,
                velocity_spike=env_req.velocity_spike,
                update_state=(
                    env_req.most_constrained_scope.value == "tenant"
                    or env_req.current_utilization_ratio < self.policy.soft_limit_ratio
                    or env_req.velocity_spike
                ),
                timestamp=now,
            )

            if not circuit.allowed or circuit.assigned_model is None:
                latency_ms = (time.perf_counter() - t0) * 1000.0
                reason = env_req.violation_reason if env_req.velocity_spike else circuit.reason
                verdict = BudgetVerdict(
                    verdict_id=verdict_id,
                    allowed=False,
                    action="BLOCK",
                    circuit_state=CircuitState.OPEN_HALT,
                    requested_model=model,
                    assigned_model=None,
                    downgraded=False,
                    estimated_cost_usd=0.0,
                    requested_cost_usd=requested_breakdown.total_cost_usd,
                    estimated_savings_usd=0.0,
                    estimated_tokens=est_tokens,
                    utilization_ratio=env_req.current_utilization_ratio,
                    velocity_usd_per_min=env_req.velocity_usd_per_min,
                    remaining_usd_by_scope=env_req.remaining_usd_by_scope,
                    remaining_tokens_by_scope=env_req.remaining_tokens_by_scope,
                    triggered_scope=env_req.violated_scope or env_req.most_constrained_scope,
                    reason=reason or circuit.reason,
                    latency_ms=latency_ms,
                )
                if self.telemetry is not None:
                    self.telemetry.record_verdict(verdict)
                return verdict

            assigned_model = circuit.assigned_model
            if circuit.downgraded:
                assigned_breakdown = self.catalog.calculate_cost(
                    assigned_model,
                    input_tokens=estimated_input_tokens,
                    cached_input_tokens=estimated_cached_tokens,
                    output_tokens=estimated_output_tokens,
                    reasoning_tokens=estimated_reasoning_tokens,
                    tool_calls=estimated_tool_calls,
                )
                env_final = self.limiter.check_envelope(
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                    run_id=run_id,
                    step_id=step_id,
                    estimated_cost_usd=assigned_breakdown.total_cost_usd,
                    estimated_tokens=est_tokens,
                    timestamp=now,
                )
            else:
                assigned_breakdown = requested_breakdown
                env_final = env_req

            if not env_final.within_hard_limits:
                latency_ms = (time.perf_counter() - t0) * 1000.0
                verdict = BudgetVerdict(
                    verdict_id=verdict_id,
                    allowed=False,
                    action="BLOCK",
                    circuit_state=CircuitState.OPEN_HALT,
                    requested_model=model,
                    assigned_model=None,
                    downgraded=False,
                    estimated_cost_usd=0.0,
                    requested_cost_usd=requested_breakdown.total_cost_usd,
                    estimated_savings_usd=0.0,
                    estimated_tokens=est_tokens,
                    utilization_ratio=env_final.current_utilization_ratio,
                    velocity_usd_per_min=env_final.velocity_usd_per_min,
                    remaining_usd_by_scope=env_final.remaining_usd_by_scope,
                    remaining_tokens_by_scope=env_final.remaining_tokens_by_scope,
                    triggered_scope=env_final.violated_scope,
                    reason=env_final.violation_reason or "Hard hierarchical cap violated",
                    latency_ms=latency_ms,
                )
                if self.telemetry is not None:
                    self.telemetry.record_verdict(verdict)
                return verdict

            savings = round(
                max(0.0, requested_breakdown.total_cost_usd - assigned_breakdown.total_cost_usd),
                9,
            )
            action = "DOWNGRADE" if circuit.downgraded else "ALLOW"
            latency_ms = (time.perf_counter() - t0) * 1000.0
            verdict = BudgetVerdict(
                verdict_id=verdict_id,
                allowed=True,
                action=action,
                circuit_state=circuit.state,
                requested_model=model,
                assigned_model=assigned_model,
                downgraded=circuit.downgraded,
                estimated_cost_usd=assigned_breakdown.total_cost_usd,
                requested_cost_usd=requested_breakdown.total_cost_usd,
                estimated_savings_usd=savings,
                estimated_tokens=est_tokens,
                utilization_ratio=env_final.current_utilization_ratio,
                velocity_usd_per_min=env_final.velocity_usd_per_min,
                remaining_usd_by_scope=env_final.remaining_usd_by_scope,
                remaining_tokens_by_scope=env_final.remaining_tokens_by_scope,
                triggered_scope=None,
                reason=circuit.reason,
                latency_ms=latency_ms,
            )
            if self.telemetry is not None:
                self.telemetry.record_verdict(verdict)
            return verdict

    def record_usage(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        run_id: str,
        step_id: str,
        model: str,
        input_tokens: int = 0,
        cached_input_tokens: int = 0,
        output_tokens: int = 0,
        reasoning_tokens: int = 0,
        tool_calls: int = 0,
        requested_model: str | None = None,
        downgraded: bool = False,
        timestamp: float | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> BudgetReceipt:
        """Record actual token/tool usage into limiter, circuit breaker, and SHA-256 ledger."""
        now = timestamp if timestamp is not None else time.time()
        event_id = f"BE-{next(self._event_seq):06d}"

        with self._lock:
            exec_breakdown = self.catalog.calculate_cost(
                model,
                input_tokens=input_tokens,
                cached_input_tokens=cached_input_tokens,
                output_tokens=output_tokens,
                reasoning_tokens=reasoning_tokens,
                tool_calls=tool_calls,
            )

            eff_requested = requested_model or model
            is_downgraded = downgraded or (
                eff_requested.strip().lower() != model.strip().lower()
            )
            savings = 0.0
            if is_downgraded:
                req_breakdown = self.catalog.calculate_cost(
                    eff_requested,
                    input_tokens=input_tokens,
                    cached_input_tokens=cached_input_tokens,
                    output_tokens=output_tokens,
                    reasoning_tokens=reasoning_tokens,
                    tool_calls=tool_calls,
                )
                savings = round(
                    max(0.0, req_breakdown.total_cost_usd - exec_breakdown.total_cost_usd),
                    9,
                )

            event = SpendEvent(
                event_id=event_id,
                tenant_id=tenant_id,
                agent_id=agent_id,
                run_id=run_id,
                step_id=step_id,
                model=model,
                input_tokens=input_tokens,
                cached_input_tokens=cached_input_tokens,
                output_tokens=output_tokens,
                reasoning_tokens=reasoning_tokens,
                tool_calls=tool_calls,
                cost_usd=exec_breakdown.total_cost_usd,
                timestamp=now,
                requested_model=eff_requested,
                downgraded=is_downgraded,
                estimated_savings_usd=savings,
                metadata=metadata or {},
            )

            cumulative_tenant_usd = self.limiter.record_spend(event)
            tenant_metrics = self.limiter.get_tenant_metrics(tenant_id, timestamp=now)
            velocity_spike = (
                self.policy.block_on_velocity_spike
                and tenant_metrics["velocity_usd_per_min"] > self.policy.max_usd_per_minute + 1e-9
            )

            decision = self.circuit_breaker.evaluate(
                tenant_id=tenant_id,
                requested_model=eff_requested,
                utilization_ratio=tenant_metrics["utilization_ratio"],
                velocity_spike=velocity_spike,
                update_state=True,
                timestamp=now,
            )

            receipt = self.ledger.append(
                event,
                circuit_state=decision.state,
                cumulative_tenant_usd=cumulative_tenant_usd,
            )
            if self.telemetry is not None:
                self.telemetry.record_receipt(receipt)
            return receipt

    def get_tenant_summary(
        self,
        tenant_id: str,
        *,
        timestamp: float | None = None,
    ) -> dict[str, Any]:
        """Return live FinOps spend, remaining envelope, velocity, and circuit state."""
        with self._lock:
            metrics = self.limiter.get_tenant_metrics(tenant_id, timestamp=timestamp)
            state = self.circuit_breaker.get_state(tenant_id)
            return {
                "tenant_id": tenant_id,
                "policy_id": self.policy.policy_id,
                "circuit_state": state.value,
                **metrics,
            }

    def verify_ledger(self) -> dict[str, Any]:
        """Cryptographically verify the entire SHA-256 hash-chained spend ledger."""
        with self._lock:
            return self.ledger.verify().to_dict()
