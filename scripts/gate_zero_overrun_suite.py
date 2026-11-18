"""CI gate script verifying 0.0% budget overrun, 100% gate accuracy, and ledger integrity."""

from __future__ import annotations

from agent_budget_governor.tools.bench_matrix import BudgetBenchMatrixRunner
from agent_budget_governor.tools.doctor import run_doctor


def main() -> int:
    doc = run_doctor()
    if not doc["ok"]:
        print("Doctor gate FAILED:", doc)
        return 1

    matrix = BudgetBenchMatrixRunner().run(iterations=50)
    print(
        f"[gate] accuracy={matrix['gate_accuracy_pct']}% "
        f"overrun={matrix['overrun_rate_pct']}% "
        f"throughput={matrix['throughput_ops_sec']} ops/s "
        f"avg={matrix['avg_latency_ms']} ms p95={matrix['p95_latency_ms']} ms"
    )
    if matrix["gate_accuracy_pct"] < 100.0 or matrix["overrun_rate_pct"] > 0.0:
        print("Zero-overrun FinOps gate FAILED!")
        return 1
    print("Zero-overrun FinOps gate PASSED.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
