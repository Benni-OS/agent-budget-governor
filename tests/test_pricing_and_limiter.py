"""Tests for deterministic pricing catalog and hierarchical limiter."""

from __future__ import annotations

import pytest

from agent_budget_governor.core.limiter import HierarchicalLimiter
from agent_budget_governor.core.models import BudgetPolicy, BudgetScope, SpendEvent, TokenRateCard
from agent_budget_governor.core.pricing import PricingCatalog, UnknownModelError


def test_pricing_input_cached_output_reasoning_and_tool_fees() -> None:
    catalog = PricingCatalog()
    breakdown = catalog.calculate_cost(
        "gpt-4o",
        input_tokens=100_000,
        cached_input_tokens=200_000,
        output_tokens=25_000,
        reasoning_tokens=10_000,
        tool_calls=4,
    )
    # 100k*2.50/1M = 0.25; 200k*1.25/1M = 0.25; 25k*10/1M = 0.25; 10k*10/1M = 0.10; 4*0.001 = 0.004
    assert breakdown.input_cost_usd == pytest.approx(0.25)
    assert breakdown.cached_input_cost_usd == pytest.approx(0.25)
    assert breakdown.output_cost_usd == pytest.approx(0.25)
    assert breakdown.reasoning_cost_usd == pytest.approx(0.10)
    assert breakdown.tool_call_cost_usd == pytest.approx(0.004)
    assert breakdown.total_cost_usd == pytest.approx(0.854)
    assert breakdown.total_tokens == 335_000
    assert breakdown.is_local is False


def test_pricing_distinct_reasoning_rate_and_local_zero_tier() -> None:
    catalog = PricingCatalog()
    flash = catalog.calculate_cost(
        "gemini-2.5-flash",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        reasoning_tokens=1_000_000,
    )
    # 0.15 + 0.60 + 2.50 = 3.25
    assert flash.total_cost_usd == pytest.approx(3.25)

    local = catalog.calculate_cost(
        "qwen3-30b-a3b-local",
        input_tokens=500_000,
        cached_input_tokens=200_000,
        output_tokens=100_000,
        reasoning_tokens=50_000,
        tool_calls=10,
    )
    assert local.is_local is True
    assert local.total_cost_usd == 0.0

    # Convention-based unregistered local model resolves to $0.00
    custom_local = catalog.calculate_cost("local/mistral-small-24b", input_tokens=10_000, output_tokens=5_000)
    assert custom_local.is_local is True
    assert custom_local.total_cost_usd == 0.0


def test_pricing_custom_rate_card_and_strict_unknown_error() -> None:
    catalog = PricingCatalog()
    with pytest.raises(UnknownModelError):
        catalog.get_rate_card("unregistered-cloud-model")

    catalog.register_rate_card(
        TokenRateCard(
            model="custom-pro-1",
            provider="custom",
            input_per_1m_usd=2.0,
            cached_input_per_1m_usd=0.5,
            output_per_1m_usd=8.0,
            tool_call_fee_usd=0.01,
        )
    )
    cost = catalog.calculate_cost("custom-pro-1", input_tokens=500_000, output_tokens=250_000, tool_calls=2)
    # 1.0 + 2.0 + 0.02 = 3.02
    assert cost.total_cost_usd == pytest.approx(3.02)


def test_hierarchical_limiter_enforces_step_run_agent_and_tenant_caps() -> None:
    policy = BudgetPolicy(
        per_step_usd_cap=0.20,
        per_run_usd_cap=0.35,
        per_agent_usd_cap=0.50,
        per_tenant_usd_cap=0.80,
        max_usd_per_minute=10.0,
    )
    limiter = HierarchicalLimiter(policy=policy)
    ts = 1000.0

    # Step cap violated directly
    ev_step = limiter.check_envelope(
        tenant_id="t1",
        agent_id="a1",
        run_id="r1",
        step_id="s1",
        estimated_cost_usd=0.25,
        estimated_tokens=1_000,
        timestamp=ts,
    )
    assert not ev_step.within_hard_limits
    assert ev_step.violated_scope == BudgetScope.STEP

    # Commit $0.18 in r1/s1, $0.15 in r1/s2 -> total run = $0.33
    limiter.record_spend(
        SpendEvent("e1", "t1", "a1", "r1", "s1", "gpt-4o", input_tokens=1000, cost_usd=0.18, timestamp=ts)
    )
    limiter.record_spend(
        SpendEvent("e2", "t1", "a1", "r1", "s2", "gpt-4o", input_tokens=1000, cost_usd=0.15, timestamp=ts + 1)
    )

    # Next $0.05 in r1 exceeds per_run_usd_cap ($0.33 + $0.05 = $0.38 > $0.35)
    ev_run = limiter.check_envelope(
        tenant_id="t1",
        agent_id="a1",
        run_id="r1",
        step_id="s3",
        estimated_cost_usd=0.05,
        estimated_tokens=500,
        timestamp=ts + 2,
    )
    assert not ev_run.within_hard_limits
    assert ev_run.violated_scope == BudgetScope.RUN

    # New run r2 under same agent a1: commit $0.15 -> agent total = $0.48
    limiter.record_spend(
        SpendEvent("e3", "t1", "a1", "r2", "s1", "gpt-4o", input_tokens=1000, cost_usd=0.15, timestamp=ts + 3)
    )
    ev_agent = limiter.check_envelope(
        tenant_id="t1",
        agent_id="a1",
        run_id="r2",
        step_id="s2",
        estimated_cost_usd=0.05,
        estimated_tokens=500,
        timestamp=ts + 4,
    )
    assert not ev_agent.within_hard_limits
    assert ev_agent.violated_scope == BudgetScope.AGENT


def test_limiter_velocity_spike_detection_and_window_decay() -> None:
    policy = BudgetPolicy(
        per_step_usd_cap=2.0,
        per_run_usd_cap=10.0,
        per_agent_usd_cap=10.0,
        per_tenant_usd_cap=50.0,
        velocity_window_seconds=60.0,
        max_usd_per_minute=1.00,
    )
    limiter = HierarchicalLimiter(policy=policy)

    limiter.record_spend(
        SpendEvent("e1", "t-vel", "a1", "r1", "s1", "gpt-4o", input_tokens=1000, cost_usd=0.80, timestamp=100.0)
    )
    # 5s later, trying to spend $0.30 pushes 60s burn rate to $1.10/min > $1.00/min
    spike = limiter.check_envelope(
        tenant_id="t-vel",
        agent_id="a1",
        run_id="r1",
        step_id="s2",
        estimated_cost_usd=0.30,
        estimated_tokens=500,
        timestamp=105.0,
    )
    assert spike.velocity_spike is True
    assert not spike.within_hard_limits
    assert spike.projected_velocity_usd_per_min == pytest.approx(1.10)

    # 65s later (after window decays), the $0.30 call is allowed cleanly
    recovered = limiter.check_envelope(
        tenant_id="t-vel",
        agent_id="a1",
        run_id="r1",
        step_id="s2",
        estimated_cost_usd=0.30,
        estimated_tokens=500,
        timestamp=165.0,
    )
    assert recovered.velocity_spike is False
    assert recovered.within_hard_limits is True


def test_pricing_and_policy_validation_rejects_invalid_inputs() -> None:
    catalog = PricingCatalog()
    with pytest.raises(ValueError):
        catalog.calculate_cost("gpt-4o", input_tokens=-1)

    with pytest.raises(ValueError):
        BudgetPolicy(soft_limit_ratio=1.2, hard_limit_ratio=1.0)

    with pytest.raises(ValueError):
        BudgetPolicy(per_tenant_usd_cap=0.0)

