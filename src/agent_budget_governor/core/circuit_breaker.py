"""Economic circuit breaker with automatic frontier-to-optimal/local downgrade."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any

from agent_budget_governor.core.models import BudgetPolicy, CircuitState
from agent_budget_governor.core.pricing import PricingCatalog

DEFAULT_DOWNGRADE_MAP: dict[str, str] = {
    "gpt-4o": "qwen3-30b-a3b-local",
    "o3": "qwen3-30b-a3b-local",
    "claude-3-7-sonnet": "gemini-flash",
    "claude-opus-4": "gemini-flash",
    "gemini-2.5-pro": "gemini-flash",
    "gpt-4o-mini": "qwen3-30b-a3b-local",
    "claude-3-5-haiku": "qwen3-30b-a3b-local",
    "gemini-flash": "qwen3-30b-a3b-local",
    "gemini-2.5-flash": "qwen3-30b-a3b-local",
}


@dataclass(frozen=True)
class CircuitDecision:
    """Result of evaluating the economic circuit breaker for a tenant & model."""

    state: CircuitState
    allowed: bool
    requested_model: str
    assigned_model: str | None
    downgraded: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "allowed": self.allowed,
            "requested_model": self.requested_model,
            "assigned_model": self.assigned_model,
            "downgraded": self.downgraded,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class CircuitTransition:
    """Immutable audit log entry when a tenant's circuit state changes."""

    tenant_id: str
    from_state: CircuitState
    to_state: CircuitState
    utilization_ratio: float
    reason: str
    timestamp: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "from_state": self.from_state.value,
            "to_state": self.to_state.value,
            "utilization_ratio": round(self.utilization_ratio, 6),
            "reason": self.reason,
            "timestamp": round(self.timestamp, 6),
        }


class EconomicCircuitBreaker:
    """Three-state FinOps circuit breaker (CLOSED -> SOFT_DOWNGRADE -> OPEN_HALT)."""

    def __init__(
        self,
        policy: BudgetPolicy | None = None,
        catalog: PricingCatalog | None = None,
    ) -> None:
        self.policy = policy or BudgetPolicy()
        self.catalog = catalog or PricingCatalog()
        self._lock = threading.RLock()
        self._states: dict[str, CircuitState] = {}
        self._manual_halt: dict[str, str] = {}
        self._transitions: list[CircuitTransition] = []

        self._downgrade_map: dict[str, str] = dict(DEFAULT_DOWNGRADE_MAP)
        for src, dst in self.policy.downgrade_map.items():
            self._downgrade_map[src.strip().lower()] = dst

    def resolve_downgrade(self, model: str) -> str:
        """Select optimal or local fallback model when SOFT_DOWNGRADE is active."""
        key = model.strip().lower()
        try:
            card = self.catalog.get_rate_card(model)
            if card.is_local:
                return card.model
        except KeyError:
            card = None

        if key in self._downgrade_map:
            candidate = self._downgrade_map[key]
            if not self.policy.allow_local_on_soft_limit:
                try:
                    cand_card = self.catalog.get_rate_card(candidate)
                    if cand_card.is_local:
                        return "gemini-flash"
                except KeyError:
                    pass
            return candidate

        if card is not None and card.fallback_model:
            if self.policy.allow_local_on_soft_limit:
                return card.fallback_model
            try:
                fb_card = self.catalog.get_rate_card(card.fallback_model)
                if not fb_card.is_local:
                    return card.fallback_model
            except KeyError:
                pass
            return "gemini-flash"

        return "qwen3-30b-a3b-local" if self.policy.allow_local_on_soft_limit else "gemini-flash"

    def get_state(self, tenant_id: str) -> CircuitState:
        with self._lock:
            if tenant_id in self._manual_halt:
                return CircuitState.OPEN_HALT
            return self._states.get(tenant_id, CircuitState.CLOSED)

    def trip(self, tenant_id: str, reason: str = "manual_halt", timestamp: float | None = None) -> None:
        ts = timestamp if timestamp is not None else time.time()
        with self._lock:
            prev = self.get_state(tenant_id)
            self._manual_halt[tenant_id] = reason
            self._states[tenant_id] = CircuitState.OPEN_HALT
            if prev != CircuitState.OPEN_HALT:
                self._transitions.append(
                    CircuitTransition(
                        tenant_id=tenant_id,
                        from_state=prev,
                        to_state=CircuitState.OPEN_HALT,
                        utilization_ratio=self.policy.hard_limit_ratio,
                        reason=reason,
                        timestamp=ts,
                    )
                )

    def reset(self, tenant_id: str, timestamp: float | None = None) -> None:
        ts = timestamp if timestamp is not None else time.time()
        with self._lock:
            prev = self.get_state(tenant_id)
            self._manual_halt.pop(tenant_id, None)
            self._states[tenant_id] = CircuitState.CLOSED
            if prev != CircuitState.CLOSED:
                self._transitions.append(
                    CircuitTransition(
                        tenant_id=tenant_id,
                        from_state=prev,
                        to_state=CircuitState.CLOSED,
                        utilization_ratio=0.0,
                        reason="manual_reset",
                        timestamp=ts,
                    )
                )

    def _record_transition(
        self,
        tenant_id: str,
        new_state: CircuitState,
        utilization_ratio: float,
        reason: str,
        timestamp: float,
    ) -> None:
        prev = self._states.get(tenant_id, CircuitState.CLOSED)
        self._states[tenant_id] = new_state
        if prev != new_state:
            self._transitions.append(
                CircuitTransition(
                    tenant_id=tenant_id,
                    from_state=prev,
                    to_state=new_state,
                    utilization_ratio=utilization_ratio,
                    reason=reason,
                    timestamp=timestamp,
                )
            )

    def evaluate(
        self,
        *,
        tenant_id: str,
        requested_model: str,
        utilization_ratio: float,
        velocity_spike: bool = False,
        update_state: bool = True,
        timestamp: float | None = None,
    ) -> CircuitDecision:
        """Evaluate circuit state and model routing for the given utilization & velocity."""
        ts = timestamp if timestamp is not None else time.time()

        with self._lock:
            if tenant_id in self._manual_halt:
                reason = self._manual_halt[tenant_id]
                return CircuitDecision(
                    state=CircuitState.OPEN_HALT,
                    allowed=False,
                    requested_model=requested_model,
                    assigned_model=None,
                    downgraded=False,
                    reason=f"Circuit manually tripped: {reason}",
                )

            if velocity_spike:
                reason = "Burn-rate velocity limit breached"
                if update_state:
                    self._record_transition(
                        tenant_id,
                        CircuitState.OPEN_HALT,
                        utilization_ratio,
                        reason,
                        ts,
                    )
                return CircuitDecision(
                    state=CircuitState.OPEN_HALT,
                    allowed=False,
                    requested_model=requested_model,
                    assigned_model=None,
                    downgraded=False,
                    reason=reason,
                )

            if utilization_ratio >= self.policy.hard_limit_ratio - 1e-12:
                reason = (
                    f"Hard limit reached ({utilization_ratio:.2%} >= "
                    f"{self.policy.hard_limit_ratio:.2%})"
                )
                if update_state:
                    self._record_transition(
                        tenant_id,
                        CircuitState.OPEN_HALT,
                        utilization_ratio,
                        reason,
                        ts,
                    )
                return CircuitDecision(
                    state=CircuitState.OPEN_HALT,
                    allowed=False,
                    requested_model=requested_model,
                    assigned_model=None,
                    downgraded=False,
                    reason=reason,
                )

            if utilization_ratio >= self.policy.soft_limit_ratio - 1e-12:
                assigned = self.resolve_downgrade(requested_model)
                downgraded = assigned.strip().lower() != requested_model.strip().lower()
                reason = (
                    f"Soft limit crossed ({utilization_ratio:.2%} >= "
                    f"{self.policy.soft_limit_ratio:.2%}); "
                    + (
                        f"auto-downgraded {requested_model} -> {assigned}"
                        if downgraded
                        else f"continuing on optimal/local tier {assigned}"
                    )
                )
                if update_state:
                    self._record_transition(
                        tenant_id,
                        CircuitState.SOFT_DOWNGRADE,
                        utilization_ratio,
                        reason,
                        ts,
                    )
                return CircuitDecision(
                    state=CircuitState.SOFT_DOWNGRADE,
                    allowed=True,
                    requested_model=requested_model,
                    assigned_model=assigned,
                    downgraded=downgraded,
                    reason=reason,
                )

            reason = f"Nominal budget utilization ({utilization_ratio:.2%})"
            if update_state:
                self._record_transition(
                    tenant_id,
                    CircuitState.CLOSED,
                    utilization_ratio,
                    reason,
                    ts,
                )
            return CircuitDecision(
                state=CircuitState.CLOSED,
                allowed=True,
                requested_model=requested_model,
                assigned_model=requested_model,
                downgraded=False,
                reason=reason,
            )

    def get_transitions(self, tenant_id: str | None = None) -> list[CircuitTransition]:
        with self._lock:
            if tenant_id is None:
                return list(self._transitions)
            return [t for t in self._transitions if t.tenant_id == tenant_id]
