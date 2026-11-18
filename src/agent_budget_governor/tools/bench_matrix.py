"""Deterministic FinOps benchmark and zero-overrun verification matrix."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from agent_budget_governor.core.engine import BudgetGovernorEngine
from agent_budget_governor.core.models import BudgetPolicy, BudgetScope, CircuitState


@dataclass(frozen=True)
class BenchScenario:
    case_id: str
    label: str
    expected_allowed: bool
    expected_circuit: CircuitState
    expected_downgraded: bool
    expected_scope: BudgetScope | None
    policy: BudgetPolicy
    pre_spend_events: tuple[dict[str, Any], ...]
    auth_call: dict[str, Any]


def _build_default_scenarios() -> tuple[BenchScenario, ...]:
    base_ts = 1_700_000_000.0
    return (
        BenchScenario(
            case_id="FINOPS-01",
            label="Nominal CLOSED frontier authorization",
            expected_allowed=True,
            expected_circuit=CircuitState.CLOSED,
            expected_downgraded=False,
            expected_scope=None,
            policy=BudgetPolicy(per_step_usd_cap=0.50, per_tenant_usd_cap=10.0),
            pre_spend_events=(),
            auth_call={
                "tenant_id": "t1",
                "agent_id": "a1",
                "run_id": "r1",
                "step_id": "s1",
                "model": "gpt-4o",
                "estimated_input_tokens": 8_000,
                "estimated_output_tokens": 2_000,
                "timestamp": base_ts,
            },
        ),
        BenchScenario(
            case_id="FINOPS-02",
            label="Cached input & reasoning token pricing envelope",
            expected_allowed=True,
            expected_circuit=CircuitState.CLOSED,
            expected_downgraded=False,
            expected_scope=None,
            policy=BudgetPolicy(per_step_usd_cap=0.50, per_tenant_usd_cap=10.0),
            pre_spend_events=(),
            auth_call={
                "tenant_id": "t2",
                "agent_id": "a1",
                "run_id": "r1",
                "step_id": "s1",
                "model": "o3",
                "estimated_input_tokens": 5_000,
                "estimated_cached_tokens": 10_000,
                "estimated_output_tokens": 1_000,
                "estimated_reasoning_tokens": 2_000,
                "estimated_tool_calls": 2,
                "timestamp": base_ts,
            },
        ),
        BenchScenario(
            case_id="FINOPS-03",
            label="Local $0.00 model tier unrestricted by USD cap",
            expected_allowed=True,
            expected_circuit=CircuitState.CLOSED,
            expected_downgraded=False,
            expected_scope=None,
            policy=BudgetPolicy(per_step_usd_cap=0.01, per_tenant_usd_cap=0.01),
            pre_spend_events=(),
            auth_call={
                "tenant_id": "t3",
                "agent_id": "a1",
                "run_id": "r1",
                "step_id": "s1",
                "model": "qwen3-30b-a3b-local",
                "estimated_input_tokens": 16_000,
                "estimated_output_tokens": 4_000,
                "timestamp": base_ts,
            },
        ),
        BenchScenario(
            case_id="FINOPS-04",
            label="Per-step USD cap overrun block",
            expected_allowed=False,
            expected_circuit=CircuitState.OPEN_HALT,
            expected_downgraded=False,
            expected_scope=BudgetScope.STEP,
            policy=BudgetPolicy(per_step_usd_cap=0.05, per_run_usd_cap=5.0, per_tenant_usd_cap=10.0),
            pre_spend_events=(),
            auth_call={
                "tenant_id": "t4",
                "agent_id": "a1",
                "run_id": "r1",
                "step_id": "s1",
                "model": "gpt-4o",
                "estimated_input_tokens": 12_000,
                "estimated_output_tokens": 8_000,
                "timestamp": base_ts,
            },
        ),
        BenchScenario(
            case_id="FINOPS-05",
            label="Per-run USD cap overrun block",
            expected_allowed=False,
            expected_circuit=CircuitState.OPEN_HALT,
            expected_downgraded=False,
            expected_scope=BudgetScope.RUN,
            policy=BudgetPolicy(
                per_step_usd_cap=0.50,
                per_run_usd_cap=0.50,
                per_agent_usd_cap=5.0,
                per_tenant_usd_cap=20.0,
                per_step_token_cap=100_000,
            ),
            pre_spend_events=(
                {
                    "tenant_id": "t5",
                    "agent_id": "a1",
                    "run_id": "r1",
                    "step_id": "s1",
                    "model": "gpt-4o",
                    "input_tokens": 40_000,
                    "output_tokens": 25_000,
                    "timestamp": base_ts,
                },
            ),
            auth_call={
                "tenant_id": "t5",
                "agent_id": "a1",
                "run_id": "r1",
                "step_id": "s2",
                "model": "gpt-4o",
                "estimated_input_tokens": 20_000,
                "estimated_output_tokens": 15_000,
                "timestamp": base_ts + 1.0,
            },
        ),
        BenchScenario(
            case_id="FINOPS-06",
            label="Per-agent USD cap overrun block",
            expected_allowed=False,
            expected_circuit=CircuitState.OPEN_HALT,
            expected_downgraded=False,
            expected_scope=BudgetScope.AGENT,
            policy=BudgetPolicy(
                per_step_usd_cap=1.00,
                per_run_usd_cap=1.00,
                per_agent_usd_cap=0.60,
                per_tenant_usd_cap=20.0,
                per_step_token_cap=100_000,
            ),
            pre_spend_events=(
                {
                    "tenant_id": "t6",
                    "agent_id": "a1",
                    "run_id": "r1",
                    "step_id": "s1",
                    "model": "gpt-4o",
                    "input_tokens": 40_000,
                    "output_tokens": 30_000,
                    "timestamp": base_ts,
                },
            ),
            auth_call={
                "tenant_id": "t6",
                "agent_id": "a1",
                "run_id": "r2",
                "step_id": "s1",
                "model": "gpt-4o",
                "estimated_input_tokens": 20_000,
                "estimated_output_tokens": 20_000,
                "timestamp": base_ts + 1.0,
            },
        ),
        BenchScenario(
            case_id="FINOPS-07",
            label="Per-tenant token ceiling enforcement",
            expected_allowed=False,
            expected_circuit=CircuitState.OPEN_HALT,
            expected_downgraded=False,
            expected_scope=BudgetScope.TENANT,
            policy=BudgetPolicy(
                per_step_token_cap=50_000,
                per_run_token_cap=50_000,
                per_agent_token_cap=50_000,
                per_tenant_token_cap=10_000,
            ),
            pre_spend_events=(
                {
                    "tenant_id": "t7",
                    "agent_id": "a1",
                    "run_id": "r1",
                    "step_id": "s1",
                    "model": "qwen3-30b-a3b-local",
                    "input_tokens": 6_000,
                    "output_tokens": 1_500,
                    "timestamp": base_ts,
                },
            ),
            auth_call={
                "tenant_id": "t7",
                "agent_id": "a1",
                "run_id": "r1",
                "step_id": "s2",
                "model": "qwen3-30b-a3b-local",
                "estimated_input_tokens": 3_000,
                "estimated_output_tokens": 1_000,
                "timestamp": base_ts + 1.0,
            },
        ),
        BenchScenario(
            case_id="FINOPS-08",
            label="Burn-rate USD/min velocity spike guard",
            expected_allowed=False,
            expected_circuit=CircuitState.OPEN_HALT,
            expected_downgraded=False,
            expected_scope=BudgetScope.TENANT,
            policy=BudgetPolicy(
                per_step_usd_cap=2.0,
                per_run_usd_cap=10.0,
                per_agent_usd_cap=10.0,
                per_tenant_usd_cap=100.0,
                max_usd_per_minute=1.00,
                velocity_window_seconds=60.0,
            ),
            pre_spend_events=(
                {
                    "tenant_id": "t8",
                    "agent_id": "a1",
                    "run_id": "r1",
                    "step_id": "s1",
                    "model": "gpt-4o",
                    "input_tokens": 100_000,
                    "output_tokens": 70_000,  # $0.95 in 1s
                    "timestamp": base_ts,
                },
            ),
            auth_call={
                "tenant_id": "t8",
                "agent_id": "a1",
                "run_id": "r1",
                "step_id": "s2",
                "model": "gpt-4o",
                "estimated_input_tokens": 20_000,
                "estimated_output_tokens": 10_000,  # +$0.15 -> $1.10/min > $1.00/min
                "timestamp": base_ts + 2.0,
            },
        ),
        BenchScenario(
            case_id="FINOPS-09",
            label="SOFT_DOWNGRADE auto-reroute (gpt-4o -> qwen3-30b-a3b-local)",
            expected_allowed=True,
            expected_circuit=CircuitState.SOFT_DOWNGRADE,
            expected_downgraded=True,
            expected_scope=None,
            policy=BudgetPolicy(
                per_step_usd_cap=1.00,
                per_run_usd_cap=1.00,
                per_agent_usd_cap=1.00,
                per_tenant_usd_cap=1.00,
                max_usd_per_minute=20.0,
                soft_limit_ratio=0.80,
                hard_limit_ratio=1.00,
            ),
            pre_spend_events=(
                {
                    "tenant_id": "t9",
                    "agent_id": "a1",
                    "run_id": "r1",
                    "step_id": "s1",
                    "model": "gpt-4o",
                    "input_tokens": 100_000,
                    "output_tokens": 60_000,  # $0.85 -> 85%
                    "timestamp": base_ts,
                },
            ),
            auth_call={
                "tenant_id": "t9",
                "agent_id": "a1",
                "run_id": "r1",
                "step_id": "s2",
                "model": "gpt-4o",
                "estimated_input_tokens": 12_000,
                "estimated_output_tokens": 4_000,
                "timestamp": base_ts + 1.0,
            },
        ),
        BenchScenario(
            case_id="FINOPS-10",
            label="OPEN_HALT hard block at 100% tenant quota",
            expected_allowed=False,
            expected_circuit=CircuitState.OPEN_HALT,
            expected_downgraded=False,
            expected_scope=BudgetScope.TENANT,
            policy=BudgetPolicy(
                per_step_usd_cap=2.00,
                per_run_usd_cap=2.00,
                per_agent_usd_cap=2.00,
                per_tenant_usd_cap=1.00,
                max_usd_per_minute=20.0,
                soft_limit_ratio=0.80,
                hard_limit_ratio=1.00,
            ),
            pre_spend_events=(
                {
                    "tenant_id": "t10",
                    "agent_id": "a1",
                    "run_id": "r1",
                    "step_id": "s1",
                    "model": "gpt-4o",
                    "input_tokens": 120_000,
                    "output_tokens": 70_000,  # $0.30 + $0.70 = $1.00 -> 100%
                    "timestamp": base_ts,
                },
            ),
            auth_call={
                "tenant_id": "t10",
                "agent_id": "a2",
                "run_id": "r2",
                "step_id": "s1",
                "model": "gpt-4o",
                "estimated_input_tokens": 1_000,
                "estimated_output_tokens": 500,
                "timestamp": base_ts + 1.0,
            },
        ),
    )


DEFAULT_BENCH_SCENARIOS: tuple[BenchScenario, ...] = _build_default_scenarios()


class BudgetBenchMatrixRunner:
    """Runs deterministic FinOps overrun prevention, downgrade, and latency benchmarks."""

    def __init__(self, scenarios: tuple[BenchScenario, ...] = DEFAULT_BENCH_SCENARIOS) -> None:
        self.scenarios = scenarios

    def _build_prepared_engines(self) -> list[tuple[BenchScenario, BudgetGovernorEngine]]:
        prepared: list[tuple[BenchScenario, BudgetGovernorEngine]] = []
        for sc in self.scenarios:
            eng = BudgetGovernorEngine(policy=sc.policy)
            for ev in sc.pre_spend_events:
                eng.record_usage(**ev)
            prepared.append((sc, eng))
        return prepared

    def run(self, iterations: int = 100) -> dict[str, Any]:
        prepared = self._build_prepared_engines()
        results: list[dict[str, Any]] = []
        passed_cases = 0
        overruns = 0

        # Accuracy & overrun pass
        for sc, eng in prepared:
            verdict = eng.authorize_call(**sc.auth_call)
            case_ok = (
                verdict.allowed == sc.expected_allowed
                and verdict.circuit_state == sc.expected_circuit
                and verdict.downgraded == sc.expected_downgraded
                and (sc.expected_scope is None or verdict.triggered_scope == sc.expected_scope)
            )
            if case_ok:
                passed_cases += 1
            if not sc.expected_allowed and verdict.allowed:
                overruns += 1

            results.append(
                {
                    "case_id": sc.case_id,
                    "label": sc.label,
                    "passed": case_ok,
                    "allowed": verdict.allowed,
                    "action": verdict.action,
                    "circuit_state": verdict.circuit_state.value,
                    "assigned_model": verdict.assigned_model,
                    "downgraded": verdict.downgraded,
                    "triggered_scope": verdict.triggered_scope.value if verdict.triggered_scope else None,
                    "latency_ms": round(verdict.latency_ms, 4),
                }
            )

        # Throughput & tail latency loop
        latencies_ms: list[float] = []
        t_start = time.perf_counter()
        total_ops = 0
        for _ in range(max(1, iterations)):
            for sc, eng in prepared:
                v = eng.authorize_call(**sc.auth_call)
                latencies_ms.append(v.latency_ms)
                total_ops += 1
        elapsed_s = max(time.perf_counter() - t_start, 1e-9)

        latencies_ms.sort()
        avg_ms = sum(latencies_ms) / len(latencies_ms)
        p95_ms = latencies_ms[min(len(latencies_ms) - 1, int(0.95 * (len(latencies_ms) - 1)))]
        p99_ms = latencies_ms[min(len(latencies_ms) - 1, int(0.99 * (len(latencies_ms) - 1)))]

        gate_accuracy_pct = (passed_cases / len(self.scenarios)) * 100.0 if self.scenarios else 100.0
        disallowed_total = sum(1 for sc in self.scenarios if not sc.expected_allowed)
        overrun_rate_pct = (overruns / disallowed_total) * 100.0 if disallowed_total else 0.0

        return {
            "total_cases": len(self.scenarios),
            "passed_cases": passed_cases,
            "gate_accuracy_pct": round(gate_accuracy_pct, 2),
            "overruns": overruns,
            "overrun_rate_pct": round(overrun_rate_pct, 2),
            "iterations": iterations,
            "total_evaluations": total_ops,
            "throughput_ops_sec": round(total_ops / elapsed_s, 1),
            "avg_latency_ms": round(avg_ms, 4),
            "p95_latency_ms": round(p95_ms, 4),
            "p99_latency_ms": round(p99_ms, 4),
            "cases": results,
        }
