"""Append-only SHA-256 hash-chained spend attribution ledger."""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_budget_governor.core.models import BudgetReceipt, CircuitState, SpendEvent

GENESIS_HASH = "0" * 64


@dataclass(frozen=True)
class LedgerVerification:
    """Verification outcome for the hash-chained spend ledger."""

    valid: bool
    checked_count: int
    head_hash: str
    broken_index: int | None = None
    reason: str = "OK"

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "checked_count": self.checked_count,
            "head_hash": self.head_hash,
            "broken_index": self.broken_index,
            "reason": self.reason,
        }


def _compute_entry_hash(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _receipt_hash_payload(receipt: BudgetReceipt) -> dict[str, Any]:
    return {
        "receipt_id": receipt.receipt_id,
        "sequence": receipt.sequence,
        "event_id": receipt.event_id,
        "tenant_id": receipt.tenant_id,
        "agent_id": receipt.agent_id,
        "run_id": receipt.run_id,
        "step_id": receipt.step_id,
        "requested_model": receipt.requested_model,
        "executed_model": receipt.executed_model,
        "downgraded": receipt.downgraded,
        "circuit_state": receipt.circuit_state.value,
        "total_tokens": receipt.total_tokens,
        "tool_calls": receipt.tool_calls,
        "cost_usd": round(receipt.cost_usd, 9),
        "estimated_savings_usd": round(receipt.estimated_savings_usd, 9),
        "cumulative_tenant_usd": round(receipt.cumulative_tenant_usd, 9),
        "timestamp": round(receipt.timestamp, 6),
        "prev_hash": receipt.prev_hash,
    }


class SpendLedger:
    """Cryptographically verifiable append-only spend attribution ledger."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._receipts: list[BudgetReceipt] = []
        self._head_hash: str = GENESIS_HASH

    @property
    def head_hash(self) -> str:
        with self._lock:
            return self._head_hash

    @property
    def receipts(self) -> list[BudgetReceipt]:
        with self._lock:
            return list(self._receipts)

    def __len__(self) -> int:
        with self._lock:
            return len(self._receipts)

    def append(
        self,
        event: SpendEvent,
        *,
        circuit_state: CircuitState,
        cumulative_tenant_usd: float,
    ) -> BudgetReceipt:
        """Append a SpendEvent to the hash chain and return its sealed BudgetReceipt."""
        with self._lock:
            seq = len(self._receipts) + 1
            receipt_id = f"BR-{seq:06d}"
            prev_hash = self._head_hash
            req_model = event.requested_model or event.model
            payload = {
                "receipt_id": receipt_id,
                "sequence": seq,
                "event_id": event.event_id,
                "tenant_id": event.tenant_id,
                "agent_id": event.agent_id,
                "run_id": event.run_id,
                "step_id": event.step_id,
                "requested_model": req_model,
                "executed_model": event.model,
                "downgraded": event.downgraded,
                "circuit_state": circuit_state.value,
                "total_tokens": event.total_tokens,
                "tool_calls": event.tool_calls,
                "cost_usd": round(event.cost_usd, 9),
                "estimated_savings_usd": round(event.estimated_savings_usd, 9),
                "cumulative_tenant_usd": round(cumulative_tenant_usd, 9),
                "timestamp": round(event.timestamp, 6),
                "prev_hash": prev_hash,
            }
            entry_hash = _compute_entry_hash(payload)
            receipt = BudgetReceipt(
                receipt_id=receipt_id,
                sequence=seq,
                event_id=event.event_id,
                tenant_id=event.tenant_id,
                agent_id=event.agent_id,
                run_id=event.run_id,
                step_id=event.step_id,
                requested_model=req_model,
                executed_model=event.model,
                downgraded=event.downgraded,
                circuit_state=circuit_state,
                total_tokens=event.total_tokens,
                tool_calls=event.tool_calls,
                cost_usd=round(event.cost_usd, 9),
                estimated_savings_usd=round(event.estimated_savings_usd, 9),
                cumulative_tenant_usd=round(cumulative_tenant_usd, 9),
                timestamp=round(event.timestamp, 6),
                prev_hash=prev_hash,
                entry_hash=entry_hash,
                receipt_sha256=entry_hash,
            )
            self._receipts.append(receipt)
            self._head_hash = entry_hash
            return receipt

    @staticmethod
    def verify_receipts(receipts: list[BudgetReceipt]) -> LedgerVerification:
        """Verify sequence continuity, prev_hash links, and SHA-256 digests."""
        expected_prev = GENESIS_HASH
        for idx, receipt in enumerate(receipts):
            expected_seq = idx + 1
            if receipt.sequence != expected_seq:
                return LedgerVerification(
                    valid=False,
                    checked_count=idx,
                    head_hash=expected_prev,
                    broken_index=idx,
                    reason=f"Sequence mismatch at index {idx}: expected {expected_seq}, got {receipt.sequence}",
                )
            if receipt.prev_hash != expected_prev:
                return LedgerVerification(
                    valid=False,
                    checked_count=idx,
                    head_hash=expected_prev,
                    broken_index=idx,
                    reason=f"Chain link broken at index {idx}: prev_hash mismatch",
                )
            expected_hash = _compute_entry_hash(_receipt_hash_payload(receipt))
            if receipt.entry_hash != expected_hash or receipt.receipt_sha256 != expected_hash:
                return LedgerVerification(
                    valid=False,
                    checked_count=idx,
                    head_hash=expected_prev,
                    broken_index=idx,
                    reason=f"SHA-256 tamper detected at index {idx} ({receipt.receipt_id})",
                )
            expected_prev = expected_hash

        return LedgerVerification(
            valid=True,
            checked_count=len(receipts),
            head_hash=expected_prev,
            broken_index=None,
            reason="OK",
        )

    def verify(self) -> LedgerVerification:
        with self._lock:
            return self.verify_receipts(self._receipts)

    def replace_receipt_for_test(self, index: int, mutated: BudgetReceipt) -> None:
        """Internal/test hook allowing explicit ledger tamper verification tests."""
        with self._lock:
            self._receipts[index] = mutated

    def export_jsonl(self, path: str | Path | None = None) -> str:
        """Export ledger receipts as deterministic JSONL (and optionally write to disk)."""
        with self._lock:
            lines = [
                json.dumps(receipt.to_dict(), sort_keys=True)
                for receipt in self._receipts
            ]
            content = ("\n".join(lines) + "\n") if lines else ""
        if path is not None:
            Path(path).write_text(content, encoding="utf-8")
        return content

    @classmethod
    def parse_jsonl(cls, raw_jsonl: str) -> list[BudgetReceipt]:
        """Parse exported JSONL lines back into BudgetReceipt instances."""
        receipts: list[BudgetReceipt] = []
        for line in raw_jsonl.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            row = json.loads(stripped)
            receipts.append(
                BudgetReceipt(
                    receipt_id=row["receipt_id"],
                    sequence=int(row["sequence"]),
                    event_id=row["event_id"],
                    tenant_id=row["tenant_id"],
                    agent_id=row["agent_id"],
                    run_id=row["run_id"],
                    step_id=row["step_id"],
                    requested_model=row["requested_model"],
                    executed_model=row["executed_model"],
                    downgraded=bool(row["downgraded"]),
                    circuit_state=CircuitState(row["circuit_state"]),
                    total_tokens=int(row["total_tokens"]),
                    tool_calls=int(row["tool_calls"]),
                    cost_usd=float(row["cost_usd"]),
                    estimated_savings_usd=float(row["estimated_savings_usd"]),
                    cumulative_tenant_usd=float(row["cumulative_tenant_usd"]),
                    timestamp=float(row["timestamp"]),
                    prev_hash=row["prev_hash"],
                    entry_hash=row["entry_hash"],
                    receipt_sha256=row["receipt_sha256"],
                )
            )
        return receipts
