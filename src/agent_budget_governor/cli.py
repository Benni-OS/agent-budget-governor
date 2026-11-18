"""CLI entrypoint (`budget-governor`) for Agent Budget Governor."""

from __future__ import annotations

import argparse
import json
import time

from agent_budget_governor.core.engine import BudgetGovernorEngine
from agent_budget_governor.core.models import BudgetPolicy, TokenRateCard
from agent_budget_governor.serve.mcp_bridge import MCPBridgeServer
from agent_budget_governor.tools.bench_matrix import BudgetBenchMatrixRunner
from agent_budget_governor.tools.doctor import run_doctor


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="budget-governor",
        description="Zero-Cloud FinOps Governor & Economic Circuit Breaker for AI Agents & MCP",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_auth = sub.add_parser("authorize", help="Evaluate pre-flight call authorization")
    p_auth.add_argument("--tenant", type=str, default="tenant-alpha", help="Tenant identifier")
    p_auth.add_argument("--agent", type=str, default="planner-agent", help="Agent identifier")
    p_auth.add_argument("--run", type=str, default="run-001", help="Run identifier")
    p_auth.add_argument("--step", type=str, default="step-1", help="Step identifier")
    p_auth.add_argument("--model", type=str, default="gpt-4o", help="Requested model")
    p_auth.add_argument("--input-tokens", type=int, default=4000, help="Estimated input tokens")
    p_auth.add_argument("--cached-tokens", type=int, default=0, help="Estimated cached input tokens")
    p_auth.add_argument("--output-tokens", type=int, default=1000, help="Estimated output tokens")
    p_auth.add_argument("--reasoning-tokens", type=int, default=0, help="Estimated reasoning tokens")
    p_auth.add_argument("--tool-calls", type=int, default=1, help="Estimated tool calls")
    p_auth.add_argument("--tenant-cap", type=float, default=25.0, help="Per-tenant USD cap")
    p_auth.add_argument("--run-cap", type=float, default=2.0, help="Per-run USD cap")
    p_auth.add_argument("--step-cap", type=float, default=0.25, help="Per-step USD cap")
    p_auth.add_argument("--soft-ratio", type=float, default=0.80, help="Soft downgrade threshold ratio")
    p_auth.add_argument(
        "--spent-usd",
        type=float,
        default=0.0,
        help="Optional simulated prior spend (USD) for stateless CLI testing",
    )
    p_auth.add_argument("--json", action="store_true", help="Emit JSON verdict")

    p_sim = sub.add_parser("simulate", help="Simulate multi-step swarm budget & downgrade lifecycle")
    p_sim.add_argument("--tenant", type=str, default="acme-ai", help="Tenant identifier")
    p_sim.add_argument("--model", type=str, default="gpt-4o", help="Primary frontier model")
    p_sim.add_argument("--steps", type=int, default=12, help="Number of swarm steps to simulate")
    p_sim.add_argument("--tenant-cap", type=float, default=1.00, help="Tenant USD cap")
    p_sim.add_argument("--run-cap", type=float, default=1.00, help="Run USD cap")
    p_sim.add_argument("--step-cap", type=float, default=0.25, help="Step USD cap")
    p_sim.add_argument("--soft-ratio", type=float, default=0.80, help="Soft downgrade threshold ratio")
    p_sim.add_argument("--json", action="store_true", help="Emit JSON simulation trace")

    p_doc = sub.add_parser("doctor", help="Run 5-point FinOps governor self-diagnostics")
    p_doc.add_argument("--json", action="store_true", help="Emit JSON diagnostic report")

    p_mat = sub.add_parser("matrix", help="Run deterministic zero-overrun benchmark matrix")
    p_mat.add_argument("--iterations", type=int, default=100, help="Benchmark loop iterations")
    p_mat.add_argument("--json", action="store_true", help="Emit JSON benchmark report")

    sub.add_parser("mcp", help="Start JSON-RPC 2.0 stdio MCP bridge server")
    return parser


def _run_authorize(args: argparse.Namespace) -> int:
    policy = BudgetPolicy(
        per_step_usd_cap=args.step_cap,
        per_run_usd_cap=args.run_cap,
        per_agent_usd_cap=max(args.run_cap, args.tenant_cap),
        per_tenant_usd_cap=args.tenant_cap,
        max_usd_per_minute=max(5.0, args.tenant_cap * 2.0),
        soft_limit_ratio=args.soft_ratio,
    )
    engine = BudgetGovernorEngine(policy=policy)
    now = time.time()

    if args.spent_usd > 0:
        # Seed prior tenant spend cleanly without polluting active run/agent/velocity limits.
        seed_model = "__cli_seed_tier__"
        engine.catalog.register_rate_card(
            TokenRateCard(
                model=seed_model,
                provider="cli-seed",
                tier="frontier",
                output_per_1m_usd=round(args.spent_usd * 1000.0, 9),
            )
        )
        engine.record_usage(
            tenant_id=args.tenant,
            agent_id="__prior_agent__",
            run_id="__prior_run__",
            step_id="__prior_step__",
            model=seed_model,
            output_tokens=1_000,
            timestamp=now - policy.velocity_window_seconds - 10.0,
        )

    verdict = engine.authorize_call(
        tenant_id=args.tenant,
        agent_id=args.agent,
        run_id=args.run,
        step_id=args.step,
        model=args.model,
        estimated_input_tokens=args.input_tokens,
        estimated_cached_tokens=args.cached_tokens,
        estimated_output_tokens=args.output_tokens,
        estimated_reasoning_tokens=args.reasoning_tokens,
        estimated_tool_calls=args.tool_calls,
        timestamp=now,
    )

    if args.json:
        print(json.dumps(verdict.to_dict(), indent=2))
    else:
        print(f"verdict_id     : {verdict.verdict_id}")
        print(f"action         : {verdict.action} (allowed={verdict.allowed})")
        print(f"circuit_state  : {verdict.circuit_state.value}")
        print(f"assigned_model : {verdict.assigned_model}")
        print(f"est_cost_usd   : ${verdict.estimated_cost_usd:.6f}")
        print(f"savings_usd    : ${verdict.estimated_savings_usd:.6f}")
        print(f"utilization    : {verdict.utilization_ratio:.2%}")
        print(f"latency_ms     : {verdict.latency_ms:.4f}")
        print(f"reason         : {verdict.reason}")
    return 0 if verdict.allowed else 2


def _run_simulate(args: argparse.Namespace) -> int:
    policy = BudgetPolicy(
        per_step_usd_cap=args.step_cap,
        per_run_usd_cap=args.run_cap,
        per_agent_usd_cap=args.tenant_cap,
        per_tenant_usd_cap=args.tenant_cap,
        per_run_token_cap=max(250_000, args.steps * 40_000),
        per_agent_token_cap=max(1_000_000, args.steps * 40_000),
        max_usd_per_minute=max(5.0, args.tenant_cap * 10.0),
        soft_limit_ratio=args.soft_ratio,
        hard_limit_ratio=1.00,
    )
    engine = BudgetGovernorEngine(policy=policy)
    base_ts = 1_700_000_000.0
    trace: list[dict[str, object]] = []

    for idx in range(1, max(1, args.steps) + 1):
        step_id = f"step-{idx:02d}"
        verdict = engine.authorize_call(
            tenant_id=args.tenant,
            agent_id="swarm-worker",
            run_id="sim-run-1",
            step_id=step_id,
            model=args.model,
            estimated_input_tokens=20_000,
            estimated_output_tokens=10_000,
            estimated_tool_calls=1,
            timestamp=base_ts + idx,
        )
        receipt_id = None
        if verdict.allowed and verdict.assigned_model is not None:
            receipt = engine.record_usage(
                tenant_id=args.tenant,
                agent_id="swarm-worker",
                run_id="sim-run-1",
                step_id=step_id,
                model=verdict.assigned_model,
                input_tokens=20_000,
                output_tokens=10_000,
                tool_calls=1,
                requested_model=args.model,
                downgraded=verdict.downgraded,
                timestamp=base_ts + idx + 0.1,
            )
            receipt_id = receipt.receipt_id

        trace.append(
            {
                "step_id": step_id,
                "action": verdict.action,
                "circuit_state": verdict.circuit_state.value,
                "assigned_model": verdict.assigned_model,
                "cost_usd": verdict.estimated_cost_usd,
                "savings_usd": verdict.estimated_savings_usd,
                "utilization_ratio": verdict.utilization_ratio,
                "receipt_id": receipt_id,
            }
        )

    summary = engine.get_tenant_summary(args.tenant, timestamp=base_ts + args.steps + 1)
    ledger = engine.verify_ledger()
    payload = {"summary": summary, "ledger": ledger, "trace": trace}

    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(
            f"[simulate] tenant={args.tenant} spend=${summary['spend_usd']:.4f}/"
            f"${summary['cap_usd']:.2f} savings=${summary['estimated_savings_usd']:.4f} "
            f"circuit={summary['circuit_state']} ledger_valid={ledger['valid']}"
        )
        for row in trace:
            print(
                f"  - {row['step_id']}: {row['action']:9s} [{row['circuit_state']:14s}] "
                f"model={str(row['assigned_model']):20s} cost=${float(row['cost_usd']):.4f} "
                f"util={float(row['utilization_ratio']):.1%}"
            )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "mcp":
        MCPBridgeServer().serve_stdio()
        return 0

    if args.command == "doctor":
        report = run_doctor()
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            status = "PASS" if report["ok"] else "FAIL"
            print(f"[doctor] {status} ({report['passed_count']}/{report['total_count']} checks)")
            for chk in report["checks"]:
                mark = "OK" if chk["passed"] else "FAIL"
                print(f"  - {chk['name']}: {mark} ({chk['detail']})")
        return 0 if report["ok"] else 1

    if args.command == "matrix":
        report = BudgetBenchMatrixRunner().run(iterations=args.iterations)
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            print(
                f"[matrix] accuracy={report['gate_accuracy_pct']}% "
                f"overrun={report['overrun_rate_pct']}% "
                f"throughput={report['throughput_ops_sec']} ops/s "
                f"avg={report['avg_latency_ms']} ms p95={report['p95_latency_ms']} ms"
            )
        return 0

    if args.command == "authorize":
        return _run_authorize(args)

    return _run_simulate(args)


if __name__ == "__main__":
    raise SystemExit(main())
