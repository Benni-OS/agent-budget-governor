"""Ring-buffered deterministic FinOps telemetry collector."""

from __future__ import annotations

import threading
from collections import Counter, deque
from dataclasses import dataclass
from typing import Any

from agent_budget_governor.core.models import BudgetReceipt, BudgetVerdict


@dataclass(frozen=True)
class BudgetTelemetrySnapshot:
    """Point-in-time telemetry snapshot for budget governance operations."""

    total_authorizations: int
    allowed_count: int
    downgraded_count: int
    blocked_count: int
    total_recorded_events: int
    total_spend_usd: float
    total_estimated_savings_usd: float
    total_tokens: int
    circuit_states: dict[str, int]
    triggered_scopes: dict[str, int]
    avg_latency_ms: float
    p95_latency_ms: float
    max_latency_ms: float
    recent_receipt_ids: list[str]


class BudgetTelemetryEngine:
    """Thread-safe ring-buffered telemetry engine for BudgetGovernorEngine."""

    def __init__(self, max_history: int = 256) -> None:
        self._max_history = max_history
        self._lock = threading.RLock()
        self._total_authorizations = 0
        self._allowed = 0
        self._downgraded = 0
        self._blocked = 0
        self._total_recorded = 0
        self._total_spend_usd = 0.0
        self._total_savings_usd = 0.0
        self._total_tokens = 0
        self._circuit_states: Counter[str] = Counter()
        self._triggered_scopes: Counter[str] = Counter()
        self._latencies: deque[float] = deque(maxlen=max_history)
        self._recent_receipt_ids: deque[str] = deque(maxlen=max_history)

    def record_verdict(self, verdict: BudgetVerdict) -> None:
        with self._lock:
            self._total_authorizations += 1
            if verdict.allowed:
                self._allowed += 1
            else:
                self._blocked += 1
            if verdict.downgraded:
                self._downgraded += 1
            self._circuit_states[verdict.circuit_state.value] += 1
            if verdict.triggered_scope is not None:
                self._triggered_scopes[verdict.triggered_scope.value] += 1
            self._latencies.append(verdict.latency_ms)

    def record_receipt(self, receipt: BudgetReceipt) -> None:
        with self._lock:
            self._total_recorded += 1
            self._total_spend_usd = round(self._total_spend_usd + receipt.cost_usd, 9)
            self._total_savings_usd = round(
                self._total_savings_usd + receipt.estimated_savings_usd,
                9,
            )
            self._total_tokens += receipt.total_tokens
            self._recent_receipt_ids.append(receipt.receipt_id)

    def snapshot(self) -> BudgetTelemetrySnapshot:
        with self._lock:
            lats = sorted(self._latencies)
            if lats:
                avg_lat = sum(lats) / len(lats)
                p95_idx = min(len(lats) - 1, int(0.95 * (len(lats) - 1)))
                p95_lat = lats[p95_idx]
                max_lat = lats[-1]
            else:
                avg_lat = p95_lat = max_lat = 0.0

            return BudgetTelemetrySnapshot(
                total_authorizations=self._total_authorizations,
                allowed_count=self._allowed,
                downgraded_count=self._downgraded,
                blocked_count=self._blocked,
                total_recorded_events=self._total_recorded,
                total_spend_usd=round(self._total_spend_usd, 9),
                total_estimated_savings_usd=round(self._total_savings_usd, 9),
                total_tokens=self._total_tokens,
                circuit_states=dict(self._circuit_states),
                triggered_scopes=dict(self._triggered_scopes),
                avg_latency_ms=round(avg_lat, 4),
                p95_latency_ms=round(p95_lat, 4),
                max_latency_ms=round(max_lat, 4),
                recent_receipt_ids=list(self._recent_receipt_ids)[-10:],
            )

    def to_dict(self) -> dict[str, Any]:
        snap = self.snapshot()
        return {
            "total_authorizations": snap.total_authorizations,
            "allowed_count": snap.allowed_count,
            "downgraded_count": snap.downgraded_count,
            "blocked_count": snap.blocked_count,
            "total_recorded_events": snap.total_recorded_events,
            "total_spend_usd": snap.total_spend_usd,
            "total_estimated_savings_usd": snap.total_estimated_savings_usd,
            "total_tokens": snap.total_tokens,
            "circuit_states": snap.circuit_states,
            "triggered_scopes": snap.triggered_scopes,
            "avg_latency_ms": snap.avg_latency_ms,
            "p95_latency_ms": snap.p95_latency_ms,
            "max_latency_ms": snap.max_latency_ms,
            "recent_receipt_ids": snap.recent_receipt_ids,
        }
