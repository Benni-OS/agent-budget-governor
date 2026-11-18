"""Tests for the 3-state economic circuit breaker and model downgrade routing."""

from __future__ import annotations

from agent_budget_governor.core.circuit_breaker import EconomicCircuitBreaker
from agent_budget_governor.core.models import BudgetPolicy, CircuitState


def test_circuit_breaker_closed_soft_downgrade_and_open_halt_transitions() -> None:
    policy = BudgetPolicy(soft_limit_ratio=0.80, hard_limit_ratio=1.00)
    breaker = EconomicCircuitBreaker(policy=policy)

    # < 80% -> CLOSED, keeps frontier model
    d1 = breaker.evaluate(
        tenant_id="tenant-1",
        requested_model="gpt-4o",
        utilization_ratio=0.45,
        timestamp=10.0,
    )
    assert d1.state == CircuitState.CLOSED
    assert d1.allowed is True
    assert d1.assigned_model == "gpt-4o"
    assert d1.downgraded is False

    # >= 80% -> SOFT_DOWNGRADE, reroutes frontier -> local / cost-optimal
    d2 = breaker.evaluate(
        tenant_id="tenant-1",
        requested_model="gpt-4o",
        utilization_ratio=0.82,
        timestamp=20.0,
    )
    assert d2.state == CircuitState.SOFT_DOWNGRADE
    assert d2.allowed is True
    assert d2.assigned_model == "qwen3-30b-a3b-local"
    assert d2.downgraded is True

    d3 = breaker.evaluate(
        tenant_id="tenant-1",
        requested_model="claude-3-7-sonnet",
        utilization_ratio=0.90,
        timestamp=25.0,
    )
    assert d3.state == CircuitState.SOFT_DOWNGRADE
    assert d3.assigned_model == "gemini-flash"
    assert d3.downgraded is True

    # >= 100% -> OPEN_HALT, hard-blocks execution
    d4 = breaker.evaluate(
        tenant_id="tenant-1",
        requested_model="gpt-4o",
        utilization_ratio=1.00,
        timestamp=30.0,
    )
    assert d4.state == CircuitState.OPEN_HALT
    assert d4.allowed is False
    assert d4.assigned_model is None

    transitions = breaker.get_transitions("tenant-1")
    assert [t.to_state for t in transitions] == [
        CircuitState.SOFT_DOWNGRADE,
        CircuitState.OPEN_HALT,
    ]


def test_circuit_breaker_manual_trip_reset_and_custom_downgrade_map() -> None:
    policy = BudgetPolicy(
        soft_limit_ratio=0.75,
        hard_limit_ratio=1.00,
        allow_local_on_soft_limit=False,
        downgrade_map={"gpt-4o": "gpt-4o-mini"},
    )
    breaker = EconomicCircuitBreaker(policy=policy)

    d_soft = breaker.evaluate(
        tenant_id="t-custom",
        requested_model="gpt-4o",
        utilization_ratio=0.76,
    )
    assert d_soft.state == CircuitState.SOFT_DOWNGRADE
    assert d_soft.assigned_model == "gpt-4o-mini"

    # Already-local model is never marked downgraded
    d_local = breaker.evaluate(
        tenant_id="t-custom",
        requested_model="qwen3-30b-a3b-local",
        utilization_ratio=0.76,
    )
    assert d_local.allowed is True
    assert d_local.assigned_model == "qwen3-30b-a3b-local"
    assert d_local.downgraded is False

    # Manual trip holds OPEN_HALT until explicit reset
    breaker.trip("t-custom", reason="emergency_anomaly", timestamp=50.0)
    assert breaker.get_state("t-custom") == CircuitState.OPEN_HALT
    d_halted = breaker.evaluate(
        tenant_id="t-custom",
        requested_model="gpt-4o-mini",
        utilization_ratio=0.10,
        timestamp=51.0,
    )
    assert d_halted.allowed is False
    assert d_halted.state == CircuitState.OPEN_HALT

    breaker.reset("t-custom", timestamp=60.0)
    assert breaker.get_state("t-custom") == CircuitState.CLOSED
    d_restored = breaker.evaluate(
        tenant_id="t-custom",
        requested_model="gpt-4o",
        utilization_ratio=0.10,
        timestamp=61.0,
    )
    assert d_restored.allowed is True
    assert d_restored.state == CircuitState.CLOSED


def test_circuit_breaker_velocity_spike_forces_open_halt() -> None:
    breaker = EconomicCircuitBreaker(policy=BudgetPolicy(soft_limit_ratio=0.80, hard_limit_ratio=1.00))
    d_spike = breaker.evaluate(
        tenant_id="t-spike",
        requested_model="gpt-4o",
        utilization_ratio=0.20,
        velocity_spike=True,
        timestamp=10.0,
    )
    assert d_spike.allowed is False
    assert d_spike.state == CircuitState.OPEN_HALT
    assert "velocity" in d_spike.reason.lower()

