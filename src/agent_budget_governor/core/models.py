"""Typed domain models for Agent Budget Governor."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class BudgetScope(str, Enum):
    """Hierarchical scope enforcing USD and token ceilings."""

    STEP = "step"
    RUN = "run"
    AGENT = "agent"
    TENANT = "tenant"


class CircuitState(str, Enum):
    """Economic circuit breaker operational states."""

    CLOSED = "CLOSED"
    SOFT_DOWNGRADE = "SOFT_DOWNGRADE"
    OPEN_HALT = "OPEN_HALT"


@dataclass(frozen=True)
class TokenRateCard:
    """Deterministic unit pricing schedule for a model."""

    model: str
    provider: str
    tier: str = "frontier"
    input_per_1m_usd: float = 0.0
    cached_input_per_1m_usd: float = 0.0
    output_per_1m_usd: float = 0.0
    reasoning_per_1m_usd: float | None = None
    tool_call_fee_usd: float = 0.0
    is_local: bool = False
    fallback_model: str | None = None

    def __post_init__(self) -> None:
        if not self.model:
            raise ValueError("TokenRateCard.model must be non-empty.")
        for name, val in (
            ("input_per_1m_usd", self.input_per_1m_usd),
            ("cached_input_per_1m_usd", self.cached_input_per_1m_usd),
            ("output_per_1m_usd", self.output_per_1m_usd),
            ("tool_call_fee_usd", self.tool_call_fee_usd),
        ):
            if val < 0:
                raise ValueError(f"{name} must be >= 0, got {val}")
        if self.reasoning_per_1m_usd is not None and self.reasoning_per_1m_usd < 0:
            raise ValueError(f"reasoning_per_1m_usd must be >= 0, got {self.reasoning_per_1m_usd}")

    @property
    def effective_reasoning_per_1m_usd(self) -> float:
        """Return reasoning rate or fall back to standard output rate."""
        if self.reasoning_per_1m_usd is not None:
            return self.reasoning_per_1m_usd
        return self.output_per_1m_usd

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "provider": self.provider,
            "tier": self.tier,
            "input_per_1m_usd": self.input_per_1m_usd,
            "cached_input_per_1m_usd": self.cached_input_per_1m_usd,
            "output_per_1m_usd": self.output_per_1m_usd,
            "reasoning_per_1m_usd": self.effective_reasoning_per_1m_usd,
            "tool_call_fee_usd": self.tool_call_fee_usd,
            "is_local": self.is_local,
            "fallback_model": self.fallback_model,
        }


@dataclass(frozen=True)
class BudgetPolicy:
    """Declarative hierarchical budget, velocity, and circuit-breaker policy."""

    policy_id: str = "default-finops-policy"
    per_step_usd_cap: float = 0.25
    per_run_usd_cap: float = 2.00
    per_agent_usd_cap: float = 5.00
    per_tenant_usd_cap: float = 25.00
    per_step_token_cap: int = 32_000
    per_run_token_cap: int = 250_000
    per_agent_token_cap: int = 1_000_000
    per_tenant_token_cap: int = 5_000_000
    velocity_window_seconds: float = 60.0
    max_usd_per_minute: float = 5.00
    soft_limit_ratio: float = 0.80
    hard_limit_ratio: float = 1.00
    downgrade_map: dict[str, str] = field(default_factory=dict)
    allow_local_on_soft_limit: bool = True
    block_on_velocity_spike: bool = True

    def __post_init__(self) -> None:
        for name, val in (
            ("per_step_usd_cap", self.per_step_usd_cap),
            ("per_run_usd_cap", self.per_run_usd_cap),
            ("per_agent_usd_cap", self.per_agent_usd_cap),
            ("per_tenant_usd_cap", self.per_tenant_usd_cap),
            ("velocity_window_seconds", self.velocity_window_seconds),
            ("max_usd_per_minute", self.max_usd_per_minute),
        ):
            if val <= 0:
                raise ValueError(f"{name} must be > 0, got {val}")
        for name, val in (
            ("per_step_token_cap", self.per_step_token_cap),
            ("per_run_token_cap", self.per_run_token_cap),
            ("per_agent_token_cap", self.per_agent_token_cap),
            ("per_tenant_token_cap", self.per_tenant_token_cap),
        ):
            if val <= 0:
                raise ValueError(f"{name} must be > 0, got {val}")
        if not (0.0 < self.soft_limit_ratio <= self.hard_limit_ratio):
            raise ValueError(
                f"Expected 0 < soft_limit_ratio <= hard_limit_ratio, got "
                f"{self.soft_limit_ratio} and {self.hard_limit_ratio}"
            )

    def usd_cap_for(self, scope: BudgetScope) -> float:
        mapping = {
            BudgetScope.STEP: self.per_step_usd_cap,
            BudgetScope.RUN: self.per_run_usd_cap,
            BudgetScope.AGENT: self.per_agent_usd_cap,
            BudgetScope.TENANT: self.per_tenant_usd_cap,
        }
        return mapping[scope]

    def token_cap_for(self, scope: BudgetScope) -> int:
        mapping = {
            BudgetScope.STEP: self.per_step_token_cap,
            BudgetScope.RUN: self.per_run_token_cap,
            BudgetScope.AGENT: self.per_agent_token_cap,
            BudgetScope.TENANT: self.per_tenant_token_cap,
        }
        return mapping[scope]

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy_id": self.policy_id,
            "per_step_usd_cap": self.per_step_usd_cap,
            "per_run_usd_cap": self.per_run_usd_cap,
            "per_agent_usd_cap": self.per_agent_usd_cap,
            "per_tenant_usd_cap": self.per_tenant_usd_cap,
            "per_step_token_cap": self.per_step_token_cap,
            "per_run_token_cap": self.per_run_token_cap,
            "per_agent_token_cap": self.per_agent_token_cap,
            "per_tenant_token_cap": self.per_tenant_token_cap,
            "velocity_window_seconds": self.velocity_window_seconds,
            "max_usd_per_minute": self.max_usd_per_minute,
            "soft_limit_ratio": self.soft_limit_ratio,
            "hard_limit_ratio": self.hard_limit_ratio,
            "downgrade_map": dict(self.downgrade_map),
            "allow_local_on_soft_limit": self.allow_local_on_soft_limit,
            "block_on_velocity_spike": self.block_on_velocity_spike,
        }


@dataclass(frozen=True)
class SpendEvent:
    """Recorded execution consumption event attributed to a hierarchical path."""

    event_id: str
    tenant_id: str
    agent_id: str
    run_id: str
    step_id: str
    model: str
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    tool_calls: int = 0
    cost_usd: float = 0.0
    timestamp: float = field(default_factory=time.time)
    requested_model: str | None = None
    downgraded: bool = False
    estimated_savings_usd: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.cached_input_tokens
            + self.output_tokens
            + self.reasoning_tokens
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "tenant_id": self.tenant_id,
            "agent_id": self.agent_id,
            "run_id": self.run_id,
            "step_id": self.step_id,
            "model": self.model,
            "requested_model": self.requested_model or self.model,
            "downgraded": self.downgraded,
            "input_tokens": self.input_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "output_tokens": self.output_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "total_tokens": self.total_tokens,
            "tool_calls": self.tool_calls,
            "cost_usd": round(self.cost_usd, 9),
            "estimated_savings_usd": round(self.estimated_savings_usd, 9),
            "timestamp": round(self.timestamp, 6),
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class BudgetVerdict:
    """Pre-flight authorization decision emitted before model/tool dispatch."""

    verdict_id: str
    allowed: bool
    action: str
    circuit_state: CircuitState
    requested_model: str
    assigned_model: str | None
    downgraded: bool
    estimated_cost_usd: float
    requested_cost_usd: float
    estimated_savings_usd: float
    estimated_tokens: int
    utilization_ratio: float
    velocity_usd_per_min: float
    remaining_usd_by_scope: dict[str, float]
    remaining_tokens_by_scope: dict[str, int]
    triggered_scope: BudgetScope | None = None
    reason: str = "OK"
    latency_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict_id": self.verdict_id,
            "allowed": self.allowed,
            "action": self.action,
            "circuit_state": self.circuit_state.value,
            "requested_model": self.requested_model,
            "assigned_model": self.assigned_model,
            "downgraded": self.downgraded,
            "estimated_cost_usd": round(self.estimated_cost_usd, 9),
            "requested_cost_usd": round(self.requested_cost_usd, 9),
            "estimated_savings_usd": round(self.estimated_savings_usd, 9),
            "estimated_tokens": self.estimated_tokens,
            "utilization_ratio": round(self.utilization_ratio, 6),
            "velocity_usd_per_min": round(self.velocity_usd_per_min, 6),
            "remaining_usd_by_scope": {
                k: round(v, 9) for k, v in self.remaining_usd_by_scope.items()
            },
            "remaining_tokens_by_scope": dict(self.remaining_tokens_by_scope),
            "triggered_scope": self.triggered_scope.value if self.triggered_scope else None,
            "reason": self.reason,
            "latency_ms": round(self.latency_ms, 4),
        }


@dataclass(frozen=True)
class BudgetReceipt:
    """Tamper-evident SHA-256 hash-chained ledger receipt for attributed spend."""

    receipt_id: str
    sequence: int
    event_id: str
    tenant_id: str
    agent_id: str
    run_id: str
    step_id: str
    requested_model: str
    executed_model: str
    downgraded: bool
    circuit_state: CircuitState
    total_tokens: int
    tool_calls: int
    cost_usd: float
    estimated_savings_usd: float
    cumulative_tenant_usd: float
    timestamp: float
    prev_hash: str
    entry_hash: str
    receipt_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "receipt_id": self.receipt_id,
            "sequence": self.sequence,
            "event_id": self.event_id,
            "tenant_id": self.tenant_id,
            "agent_id": self.agent_id,
            "run_id": self.run_id,
            "step_id": self.step_id,
            "requested_model": self.requested_model,
            "executed_model": self.executed_model,
            "downgraded": self.downgraded,
            "circuit_state": self.circuit_state.value,
            "total_tokens": self.total_tokens,
            "tool_calls": self.tool_calls,
            "cost_usd": round(self.cost_usd, 9),
            "estimated_savings_usd": round(self.estimated_savings_usd, 9),
            "cumulative_tenant_usd": round(self.cumulative_tenant_usd, 9),
            "timestamp": round(self.timestamp, 6),
            "prev_hash": self.prev_hash,
            "entry_hash": self.entry_hash,
            "receipt_sha256": self.receipt_sha256,
        }
