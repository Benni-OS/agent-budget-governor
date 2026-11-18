"""Tests for SHA-256 hash-chained SpendLedger and BudgetGovernorEngine."""

from __future__ import annotations

import dataclasses
from pathlib import Path

from agent_budget_governor.core.engine import BudgetGovernorEngine
from agent_budget_governor.core.ledger import GENESIS_HASH, SpendLedger
from agent_budget_governor.core.models import BudgetPolicy, CircuitState


def test_engine_end_to_end_downgrade_savings_and_hard_halt() -> None:
    policy = BudgetPolicy(
        policy_id="test-zero-overrun",
        per_step_usd_cap=0.90,
        per_run_usd_cap=1.00,
        per_agent_usd_cap=1.00,
        per_tenant_usd_cap=1.00,
        per_step_token_cap=200_000,
        per_run_token_cap=500_000,
        max_usd_per_minute=20.0,
        soft_limit_ratio=0.80,
        hard_limit_ratio=1.00,
    )
    engine = BudgetGovernorEngine(policy=policy)

    # Step 1: nominal CLOSED authorization
    v1 = engine.authorize_call(
        tenant_id="acme",
        agent_id="planner",
        run_id="r1",
        step_id="s1",
        model="gpt-4o",
        estimated_input_tokens=100_000,
        estimated_output_tokens=60_000,  # $0.25 + $0.60 = $0.85 (85% of $1.00)
        timestamp=1000.0,
    )
    assert v1.allowed is True
    assert v1.circuit_state == CircuitState.CLOSED
    assert v1.assigned_model == "gpt-4o"

    r1 = engine.record_usage(
        tenant_id="acme",
        agent_id="planner",
        run_id="r1",
        step_id="s1",
        model="gpt-4o",
        input_tokens=100_000,
        output_tokens=60_000,
        timestamp=1001.0,
    )
    assert r1.cumulative_tenant_usd == 0.85
    assert r1.prev_hash == GENESIS_HASH

    # Step 2: utilization is 85% >= 80% -> SOFT_DOWNGRADE to local $0.00 tier
    v2 = engine.authorize_call(
        tenant_id="acme",
        agent_id="planner",
        run_id="r1",
        step_id="s2",
        model="gpt-4o",
        estimated_input_tokens=40_000,
        estimated_output_tokens=10_000,
        timestamp=1002.0,
    )
    assert v2.allowed is True
    assert v2.action == "DOWNGRADE"
    assert v2.circuit_state == CircuitState.SOFT_DOWNGRADE
    assert v2.assigned_model == "qwen3-30b-a3b-local"
    assert v2.downgraded is True
    assert v2.estimated_cost_usd == 0.0
    assert v2.estimated_savings_usd > 0.19

    r2 = engine.record_usage(
        tenant_id="acme",
        agent_id="planner",
        run_id="r1",
        step_id="s2",
        model=v2.assigned_model,
        input_tokens=40_000,
        output_tokens=10_000,
        requested_model="gpt-4o",
        downgraded=True,
        timestamp=1003.0,
    )
    assert r2.cost_usd == 0.0
    assert r2.prev_hash == r1.entry_hash
    assert r2.estimated_savings_usd == 0.2

    # Step 3: push to $1.00 -> OPEN_HALT blocks subsequent calls
    engine.record_usage(
        tenant_id="acme",
        agent_id="planner",
        run_id="r1",
        step_id="s3",
        model="gpt-4o",
        input_tokens=20_000,
        output_tokens=10_000,  # +$0.15 => $1.00
        timestamp=1004.0,
    )
    v3 = engine.authorize_call(
        tenant_id="acme",
        agent_id="planner",
        run_id="r1",
        step_id="s4",
        model="gpt-4o",
        estimated_input_tokens=1_000,
        estimated_output_tokens=500,
        timestamp=1005.0,
    )
    assert v3.allowed is False
    assert v3.action == "BLOCK"
    assert v3.circuit_state == CircuitState.OPEN_HALT

    summary = engine.get_tenant_summary("acme", timestamp=1006.0)
    assert summary["spend_usd"] == 1.0
    assert summary["remaining_usd"] == 0.0
    assert summary["downgraded_calls"] == 1
    assert summary["estimated_savings_usd"] == 0.2
    assert summary["circuit_state"] == "OPEN_HALT"

    verification = engine.verify_ledger()
    assert verification["valid"] is True
    assert verification["checked_count"] == 3


def test_ledger_tamper_detection_and_jsonl_roundtrip(tmp_path: Path) -> None:
    engine = BudgetGovernorEngine()
    engine.record_usage(
        tenant_id="t-ledger",
        agent_id="a1",
        run_id="r1",
        step_id="s1",
        model="gpt-4o",
        input_tokens=10_000,
        output_tokens=5_000,
        timestamp=500.0,
    )
    engine.record_usage(
        tenant_id="t-ledger",
        agent_id="a1",
        run_id="r1",
        step_id="s2",
        model="claude-3-7-sonnet",
        input_tokens=10_000,
        output_tokens=5_000,
        timestamp=501.0,
    )

    jsonl_path = tmp_path / "spend_ledger.jsonl"
    raw = engine.ledger.export_jsonl(jsonl_path)
    parsed = SpendLedger.parse_jsonl(jsonl_path.read_text(encoding="utf-8"))
    assert len(parsed) == 2
    assert SpendLedger.verify_receipts(parsed).valid is True
    assert raw.count("\n") == 2

    # Mutating a single historical receipt breaks SHA-256 chain verification
    tampered = dataclasses.replace(engine.ledger.receipts[0], cost_usd=0.0001)
    engine.ledger.replace_receipt_for_test(0, tampered)
    verdict = engine.verify_ledger()
    assert verdict["valid"] is False
    assert verdict["broken_index"] == 0
    assert "tamper detected" in verdict["reason"].lower()


def test_engine_tenant_isolation_and_velocity_spike_block() -> None:
    policy = BudgetPolicy(
        per_step_usd_cap=1.00,
        per_run_usd_cap=5.00,
        per_agent_usd_cap=5.00,
        per_tenant_usd_cap=10.00,
        per_step_token_cap=150_000,
        max_usd_per_minute=0.50,
        velocity_window_seconds=60.0,
    )
    engine = BudgetGovernorEngine(policy=policy)

    # Tenant A burns $0.45 in 1s -> next $0.10 within same minute trips velocity guard
    engine.record_usage(
        tenant_id="tenant-a",
        agent_id="a1",
        run_id="r1",
        step_id="s1",
        model="gpt-4o",
        input_tokens=60_000,
        output_tokens=30_000,
        timestamp=100.0,
    )
    blocked_a = engine.authorize_call(
        tenant_id="tenant-a",
        agent_id="a1",
        run_id="r1",
        step_id="s2",
        model="gpt-4o",
        estimated_input_tokens=20_000,
        estimated_output_tokens=10_000,
        timestamp=102.0,
    )
    assert blocked_a.allowed is False
    assert blocked_a.circuit_state == CircuitState.OPEN_HALT

    # Tenant B remains completely isolated and CLOSED
    allowed_b = engine.authorize_call(
        tenant_id="tenant-b",
        agent_id="a1",
        run_id="r1",
        step_id="s1",
        model="gpt-4o",
        estimated_input_tokens=10_000,
        estimated_output_tokens=2_000,
        timestamp=102.0,
    )
    assert allowed_b.allowed is True
    assert allowed_b.circuit_state == CircuitState.CLOSED

