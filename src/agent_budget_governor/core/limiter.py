"""Hierarchical multi-scope budget and burn-rate velocity enforcer."""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any

from agent_budget_governor.core.models import BudgetPolicy, BudgetScope, SpendEvent


@dataclass(frozen=True)
class EnvelopeEvaluation:
    """Result of checking a candidate operation against hierarchical caps."""

    within_hard_limits: bool
    current_utilization_ratio: float
    projected_utilization_ratio: float
    tenant_utilization_ratio: float
    velocity_usd_per_min: float
    projected_velocity_usd_per_min: float
    velocity_spike: bool
    remaining_usd_by_scope: dict[str, float]
    remaining_tokens_by_scope: dict[str, int]
    most_constrained_scope: BudgetScope
    violated_scope: BudgetScope | None = None
    violation_reason: str | None = None


class HierarchicalLimiter:
    """Enforces USD & token ceilings across step/run/agent/tenant scopes + burn velocity."""

    def __init__(self, policy: BudgetPolicy | None = None) -> None:
        self.policy = policy or BudgetPolicy()
        self._lock = threading.RLock()
        self._usd_totals: dict[BudgetScope, dict[tuple[str, ...], float]] = {
            scope: defaultdict(float) for scope in BudgetScope
        }
        self._token_totals: dict[BudgetScope, dict[tuple[str, ...], int]] = {
            scope: defaultdict(int) for scope in BudgetScope
        }
        self._velocity_windows: dict[str, deque[tuple[float, float]]] = defaultdict(deque)
        self._tenant_stats: dict[str, dict[str, Any]] = defaultdict(
            lambda: {
                "event_count": 0,
                "downgraded_calls": 0,
                "estimated_savings_usd": 0.0,
                "runs": defaultdict(lambda: {"spend_usd": 0.0, "tokens": 0, "events": 0}),
                "agents": defaultdict(lambda: {"spend_usd": 0.0, "tokens": 0, "events": 0}),
            }
        )

    @staticmethod
    def _scope_keys(
        tenant_id: str,
        agent_id: str,
        run_id: str,
        step_id: str,
    ) -> dict[BudgetScope, tuple[str, ...]]:
        return {
            BudgetScope.STEP: (tenant_id, agent_id, run_id, step_id),
            BudgetScope.RUN: (tenant_id, agent_id, run_id),
            BudgetScope.AGENT: (tenant_id, agent_id),
            BudgetScope.TENANT: (tenant_id,),
        }

    def _window_spend(self, tenant_id: str, now: float, *, prune: bool) -> float:
        cutoff = now - self.policy.velocity_window_seconds
        dq = self._velocity_windows[tenant_id]
        if prune:
            while dq and dq[0][0] < cutoff:
                dq.popleft()
        return round(sum(cost for ts, cost in dq if cutoff <= ts <= now + 1e-9), 9)

    def get_velocity_usd_per_min(self, tenant_id: str, timestamp: float | None = None) -> float:
        now = timestamp if timestamp is not None else time.time()
        with self._lock:
            window_usd = self._window_spend(tenant_id, now, prune=False)
            return round((window_usd / self.policy.velocity_window_seconds) * 60.0, 6)

    def check_envelope(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        run_id: str,
        step_id: str,
        estimated_cost_usd: float,
        estimated_tokens: int,
        timestamp: float | None = None,
    ) -> EnvelopeEvaluation:
        """Evaluate current + projected spend against hierarchical USD/token caps & burn rate."""
        now = timestamp if timestamp is not None else time.time()
        keys = self._scope_keys(tenant_id, agent_id, run_id, step_id)

        with self._lock:
            remaining_usd: dict[str, float] = {}
            remaining_tokens: dict[str, int] = {}

            max_current_ratio = 0.0
            max_projected_ratio = 0.0
            most_constrained = BudgetScope.TENANT
            violated_scope: BudgetScope | None = None
            violation_reason: str | None = None
            tenant_ratio = 0.0

            for scope in (
                BudgetScope.STEP,
                BudgetScope.RUN,
                BudgetScope.AGENT,
                BudgetScope.TENANT,
            ):
                key = keys[scope]
                curr_usd = self._usd_totals[scope][key]
                curr_tok = self._token_totals[scope][key]

                usd_cap = self.policy.usd_cap_for(scope)
                tok_cap = self.policy.token_cap_for(scope)
                hard_usd = usd_cap * self.policy.hard_limit_ratio
                hard_tok = tok_cap * self.policy.hard_limit_ratio

                proj_usd = round(curr_usd + estimated_cost_usd, 9)
                proj_tok = curr_tok + estimated_tokens

                curr_ratio = max(curr_usd / usd_cap, curr_tok / tok_cap)
                proj_ratio = max(proj_usd / usd_cap, proj_tok / tok_cap)

                if scope == BudgetScope.TENANT:
                    tenant_ratio = curr_ratio

                if curr_ratio >= max_current_ratio:
                    max_current_ratio = curr_ratio
                    most_constrained = scope
                if proj_ratio > max_projected_ratio:
                    max_projected_ratio = proj_ratio

                remaining_usd[scope.value] = round(max(0.0, hard_usd - curr_usd), 9)
                remaining_tokens[scope.value] = max(0, int(hard_tok) - curr_tok)

                if violated_scope is None:
                    if curr_usd >= hard_usd - 1e-12 or (
                        estimated_cost_usd > 0 and proj_usd > hard_usd + 1e-12
                    ):
                        violated_scope = scope
                        violation_reason = (
                            f"{scope.value} USD cap exceeded: current=${curr_usd:.6f}, "
                            f"projected=${proj_usd:.6f}, cap=${hard_usd:.6f}"
                        )
                    elif curr_tok >= hard_tok or (
                        estimated_tokens > 0 and proj_tok > hard_tok
                    ):
                        violated_scope = scope
                        violation_reason = (
                            f"{scope.value} token cap exceeded: current={curr_tok}, "
                            f"projected={proj_tok}, cap={int(hard_tok)}"
                        )

            window_usd = self._window_spend(tenant_id, now, prune=False)
            vel_per_min = round((window_usd / self.policy.velocity_window_seconds) * 60.0, 6)
            proj_vel_per_min = round(
                ((window_usd + estimated_cost_usd) / self.policy.velocity_window_seconds) * 60.0,
                6,
            )

            velocity_spike = False
            if self.policy.block_on_velocity_spike:
                if vel_per_min > self.policy.max_usd_per_minute + 1e-9 or (
                    estimated_cost_usd > 0
                    and proj_vel_per_min > self.policy.max_usd_per_minute + 1e-9
                ):
                    velocity_spike = True
                    if violated_scope is None:
                        violated_scope = BudgetScope.TENANT
                        violation_reason = (
                            f"velocity spike detected: current=${vel_per_min:.4f}/min, "
                            f"projected=${proj_vel_per_min:.4f}/min, "
                            f"limit=${self.policy.max_usd_per_minute:.4f}/min"
                        )

            return EnvelopeEvaluation(
                within_hard_limits=(violated_scope is None and not velocity_spike),
                current_utilization_ratio=round(max_current_ratio, 6),
                projected_utilization_ratio=round(max_projected_ratio, 6),
                tenant_utilization_ratio=round(tenant_ratio, 6),
                velocity_usd_per_min=vel_per_min,
                projected_velocity_usd_per_min=proj_vel_per_min,
                velocity_spike=velocity_spike,
                remaining_usd_by_scope=remaining_usd,
                remaining_tokens_by_scope=remaining_tokens,
                most_constrained_scope=most_constrained,
                violated_scope=violated_scope,
                violation_reason=violation_reason,
            )

    def record_spend(self, event: SpendEvent) -> float:
        """Commit spend across all 4 hierarchical scopes and sliding velocity window."""
        keys = self._scope_keys(
            event.tenant_id,
            event.agent_id,
            event.run_id,
            event.step_id,
        )
        with self._lock:
            for scope, key in keys.items():
                self._usd_totals[scope][key] = round(
                    self._usd_totals[scope][key] + event.cost_usd,
                    9,
                )
                self._token_totals[scope][key] += event.total_tokens

            self._velocity_windows[event.tenant_id].append((event.timestamp, event.cost_usd))
            self._window_spend(event.tenant_id, event.timestamp, prune=True)

            tstats = self._tenant_stats[event.tenant_id]
            tstats["event_count"] += 1
            if event.downgraded:
                tstats["downgraded_calls"] += 1
                tstats["estimated_savings_usd"] = round(
                    tstats["estimated_savings_usd"] + event.estimated_savings_usd,
                    9,
                )

            run_entry = tstats["runs"][event.run_id]
            run_entry["spend_usd"] = round(run_entry["spend_usd"] + event.cost_usd, 9)
            run_entry["tokens"] += event.total_tokens
            run_entry["events"] += 1

            agent_entry = tstats["agents"][event.agent_id]
            agent_entry["spend_usd"] = round(agent_entry["spend_usd"] + event.cost_usd, 9)
            agent_entry["tokens"] += event.total_tokens
            agent_entry["events"] += 1

            return self._usd_totals[BudgetScope.TENANT][(event.tenant_id,)]

    def get_scope_usage(
        self,
        scope: BudgetScope,
        *,
        tenant_id: str,
        agent_id: str = "",
        run_id: str = "",
        step_id: str = "",
    ) -> dict[str, Any]:
        key = self._scope_keys(tenant_id, agent_id, run_id, step_id)[scope]
        with self._lock:
            usd = self._usd_totals[scope][key]
            tok = self._token_totals[scope][key]
            usd_cap = self.policy.usd_cap_for(scope)
            tok_cap = self.policy.token_cap_for(scope)
            return {
                "scope": scope.value,
                "spend_usd": round(usd, 9),
                "cap_usd": usd_cap,
                "remaining_usd": round(max(0.0, usd_cap - usd), 9),
                "tokens_used": tok,
                "token_cap": tok_cap,
                "remaining_tokens": max(0, tok_cap - tok),
                "utilization_ratio": round(max(usd / usd_cap, tok / tok_cap), 6),
            }

    def get_tenant_metrics(
        self,
        tenant_id: str,
        timestamp: float | None = None,
    ) -> dict[str, Any]:
        now = timestamp if timestamp is not None else time.time()
        with self._lock:
            usage = self.get_scope_usage(BudgetScope.TENANT, tenant_id=tenant_id)
            vel = self.get_velocity_usd_per_min(tenant_id, timestamp=now)
            tstats = self._tenant_stats[tenant_id]
            return {
                **usage,
                "velocity_usd_per_min": vel,
                "max_usd_per_minute": self.policy.max_usd_per_minute,
                "event_count": tstats["event_count"],
                "downgraded_calls": tstats["downgraded_calls"],
                "estimated_savings_usd": round(tstats["estimated_savings_usd"], 9),
                "runs": {k: dict(v) for k, v in tstats["runs"].items()},
                "agents": {k: dict(v) for k, v in tstats["agents"].items()},
            }
