"""Benchmark script measuring zero-cloud budget governor throughput and p95/p99 latency."""

from __future__ import annotations

import json

from agent_budget_governor.tools.bench_matrix import BudgetBenchMatrixRunner


def main() -> None:
    runner = BudgetBenchMatrixRunner()
    report = runner.run(iterations=100)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
