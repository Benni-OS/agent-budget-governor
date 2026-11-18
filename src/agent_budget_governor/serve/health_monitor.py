"""Health monitor and non-destructive self-probe for Agent Budget Governor."""

from __future__ import annotations

import sys
from typing import Any

from agent_budget_governor.core.engine import BudgetGovernorEngine
from agent_budget_governor.core.models import BudgetPolicy, CircuitState
from agent_budget_governor.serve.telemetry import BudgetTelemetryEngine


class BudgetHealthMonitor:
    """Runs isolated FinOps self-probes and reports live engine + telemetry status."""

    def __init__(
        self,
        engine: BudgetGovernorEngine | None = None,
        telemetry: BudgetTelemetryEngine | None = None,
    ) -> None:
        self.telemetry = telemetry or BudgetTelemetryEngine()
        self.engine = engine or BudgetGovernorEngine(telemetry=self.telemetry)

    def check_health(self) -> dict[str, Any]:
        # Execute probe against an isolated engine instance so live tenant budgets stay clean.
        probe_policy = BudgetPolicy(
            policy_id="health-probe-policy",
            per_step_usd_cap=0.50,
            per_run_usd_cap=1.00,
            per_agent_usd_cap=1.00,
            per_tenant_usd_cap=1.00,
            max_usd_per_minute=10.00,
            soft_limit_ratio=0.80,
            hard_limit_ratio=1.00,
        )
        probe_engine = BudgetGovernorEngine(policy=probe_policy)

        v1 = probe_engine.authorize_call(
            tenant_id="probe-tenant",
            agent_id="probe-agent",
            run_id="probe-run",
            step_id="step-1",
            model="gpt-4o",
            estimated_input_tokens=10_000,
            estimated_output_tokens=2_000,
            timestamp=1000.0,
        )
        # Seed $0.85 spend (85% of $1.00 tenant cap) -> expect SOFT_DOWNGRADE
        probe_engine.record_usage(
            tenant_id="probe-tenant",
            agent_id="probe-agent",
            run_id="probe-run",
            step_id="step-1",
            model="gpt-4o",
            input_tokens=100_000,
            output_tokens=60_000,
            timestamp=1001.0,
        )
        v2 = probe_engine.authorize_call(
            tenant_id="probe-tenant",
            agent_id="probe-agent",
            run_id="probe-run",
            step_id="step-2",
            model="gpt-4o",
            estimated_input_tokens=10_000,
            estimated_output_tokens=2_000,
            timestamp=1002.0,
        )
        # Push to >= $1.00 (100% cap) -> expect OPEN_HALT
        probe_engine.record_usage(
            tenant_id="probe-tenant",
            agent_id="probe-agent",
            run_id="probe-run",
            step_id="step-3",
            model="gpt-4o",
            input_tokens=40_000,
            output_tokens=10_000,
            timestamp=1003.0,
        )
        v3 = probe_engine.authorize_call(
            tenant_id="probe-tenant",
            agent_id="probe-agent",
            run_id="probe-run",
            step_id="step-4",
            model="gpt-4o",
            estimated_input_tokens=1_000,
            estimated_output_tokens=500,
            timestamp=1004.0,
        )
        probe_ledger = probe_engine.verify_ledger()
        live_ledger = self.engine.verify_ledger()

        probe_ok = (
            v1.allowed
            and v1.circuit_state == CircuitState.CLOSED
            and v2.allowed
            and v2.circuit_state == CircuitState.SOFT_DOWNGRADE
            and v2.downgraded
            and not v3.allowed
            and v3.circuit_state == CircuitState.OPEN_HALT
            and probe_ledger["valid"]
            and live_ledger["valid"]
        )

        return {
            "status": "healthy" if probe_ok else "degraded",
            "python_version": sys.version.split()[0],
            "zero_third_party_deps": True,
            "registered_models": len(self.engine.catalog.list_models()),
            "policy_id": self.engine.policy.policy_id,
            "probe_passed": probe_ok,
            "live_ledger_valid": live_ledger["valid"],
            "live_ledger_entries": live_ledger["checked_count"],
            "telemetry": self.telemetry.to_dict(),
        }
