"""Diagnostic doctor for Agent Budget Governor."""

from __future__ import annotations

import dataclasses
import sys
from typing import Any

from agent_budget_governor.core.engine import BudgetGovernorEngine
from agent_budget_governor.core.models import BudgetPolicy, CircuitState
from agent_budget_governor.core.pricing import PricingCatalog
from agent_budget_governor.serve.health_monitor import BudgetHealthMonitor


def run_doctor() -> dict[str, Any]:
    """Run deterministic 5-point self-checks across all FinOps governor layers."""
    checks: list[dict[str, Any]] = []

    # 1. Python version check (>= 3.10)
    py_ok = sys.version_info >= (3, 10)
    checks.append(
        {
            "name": "python_version",
            "passed": py_ok,
            "detail": sys.version.split()[0],
        }
    )

    # 2. Health monitor probe
    monitor = BudgetHealthMonitor()
    health = monitor.check_health()
    checks.append(
        {
            "name": "health_probe",
            "passed": health["status"] == "healthy",
            "detail": f"models={health['registered_models']}, status={health['status']}",
        }
    )

    # 3. Deterministic pricing & $0.00 local model tier check
    catalog = PricingCatalog()
    frontier = catalog.calculate_cost(
        "gpt-4o",
        input_tokens=10_000,
        cached_input_tokens=4_000,
        output_tokens=2_000,
        reasoning_tokens=1_000,
        tool_calls=2,
    )
    local = catalog.calculate_cost(
        "qwen3-30b-a3b-local",
        input_tokens=50_000,
        output_tokens=10_000,
        tool_calls=5,
    )
    # 10k*2.50/1M=0.025 + 4k*1.25/1M=0.005 + 2k*10/1M=0.02 + 1k*10/1M=0.01 + 2*0.001=0.002 => 0.062
    pricing_ok = (
        abs(frontier.total_cost_usd - 0.062) < 1e-9
        and local.total_cost_usd == 0.0
        and local.is_local
    )
    checks.append(
        {
            "name": "pricing_and_local_zero_cost",
            "passed": pricing_ok,
            "detail": f"frontier=${frontier.total_cost_usd:.4f}, local=${local.total_cost_usd:.2f}",
        }
    )

    # 4. Circuit breaker auto-downgrade (80%) & hard halt (100%) with 0% overrun
    policy = BudgetPolicy(
        per_step_usd_cap=0.50,
        per_run_usd_cap=1.00,
        per_agent_usd_cap=1.00,
        per_tenant_usd_cap=1.00,
        max_usd_per_minute=20.00,
        soft_limit_ratio=0.80,
        hard_limit_ratio=1.00,
    )
    engine = BudgetGovernorEngine(policy=policy)
    # Record $0.85 on gpt-4o -> 100k input ($0.25) + 60k output ($0.60) = $0.85
    engine.record_usage(
        tenant_id="doc-tenant",
        agent_id="doc-agent",
        run_id="doc-run",
        step_id="s1",
        model="gpt-4o",
        input_tokens=100_000,
        output_tokens=60_000,
        timestamp=100.0,
    )
    soft_verdict = engine.authorize_call(
        tenant_id="doc-tenant",
        agent_id="doc-agent",
        run_id="doc-run",
        step_id="s2",
        model="gpt-4o",
        estimated_input_tokens=10_000,
        estimated_output_tokens=2_000,
        timestamp=101.0,
    )
    # Push spend to $1.00 (100%)
    engine.record_usage(
        tenant_id="doc-tenant",
        agent_id="doc-agent",
        run_id="doc-run",
        step_id="s3",
        model="gpt-4o",
        input_tokens=20_000,
        output_tokens=10_000,
        timestamp=102.0,
    )
    halt_verdict = engine.authorize_call(
        tenant_id="doc-tenant",
        agent_id="doc-agent",
        run_id="doc-run",
        step_id="s4",
        model="gpt-4o",
        estimated_input_tokens=1_000,
        estimated_output_tokens=500,
        timestamp=103.0,
    )
    breaker_ok = (
        soft_verdict.allowed
        and soft_verdict.circuit_state == CircuitState.SOFT_DOWNGRADE
        and soft_verdict.assigned_model == "qwen3-30b-a3b-local"
        and not halt_verdict.allowed
        and halt_verdict.circuit_state == CircuitState.OPEN_HALT
    )
    checks.append(
        {
            "name": "circuit_breaker_downgrade_and_halt",
            "passed": breaker_ok,
            "detail": (
                f"soft={soft_verdict.assigned_model}({soft_verdict.circuit_state.value}), "
                f"halt={halt_verdict.action}({halt_verdict.circuit_state.value})"
            ),
        }
    )

    # 5. SHA-256 ledger integrity & tamper detection
    valid_verify = engine.verify_ledger()
    tampered_receipt = dataclasses.replace(engine.ledger.receipts[0], cost_usd=999.99)
    engine.ledger.replace_receipt_for_test(0, tampered_receipt)
    tampered_verify = engine.verify_ledger()
    ledger_ok = (
        valid_verify["valid"]
        and valid_verify["checked_count"] == 2
        and not tampered_verify["valid"]
        and tampered_verify["broken_index"] == 0
    )
    checks.append(
        {
            "name": "sha256_ledger_tamper_detection",
            "passed": ledger_ok,
            "detail": f"valid_entries={valid_verify['checked_count']}, tamper_caught={not tampered_verify['valid']}",
        }
    )

    all_passed = all(c["passed"] for c in checks)
    return {
        "ok": all_passed,
        "checks": checks,
        "passed_count": sum(1 for c in checks if c["passed"]),
        "total_count": len(checks),
    }
