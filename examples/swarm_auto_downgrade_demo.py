"""Example demonstrating automatic frontier-to-local downgrade at 80% spend and hard halt at 100%."""

from __future__ import annotations

import json

from agent_budget_governor import BudgetGovernorEngine, BudgetPolicy


def main() -> None:
    policy = BudgetPolicy(
        policy_id="swarm-auto-downgrade",
        per_step_usd_cap=0.50,
        per_run_usd_cap=1.00,
        per_agent_usd_cap=1.00,
        per_tenant_usd_cap=1.00,
        per_step_token_cap=100_000,
        per_run_token_cap=500_000,
        max_usd_per_minute=20.00,
        soft_limit_ratio=0.80,
        hard_limit_ratio=1.00,
    )
    engine = BudgetGovernorEngine(policy=policy)
    base_ts = 1_700_000_000.0

    workload = [
        ("planner-1", "gpt-4o", 40_000, 20_000),            # $0.30 (30%)
        ("coder-1", "claude-3-7-sonnet", 40_000, 20_000),   # +$0.42 -> $0.72 (72%)
        ("reviewer-1", "gpt-4o", 20_000, 10_000),           # +$0.15 -> $0.87 (87% >= 80%)
        ("planner-2", "gpt-4o", 20_000, 10_000),            # Auto-downgraded -> qwen3-30b-a3b-local ($0.00!)
        ("coder-2", "claude-3-7-sonnet", 10_000, 5_000),    # Auto-downgraded -> gemini-flash ($0.003)
    ]

    for idx, (agent_id, req_model, in_tok, out_tok) in enumerate(workload, start=1):
        verdict = engine.authorize_call(
            tenant_id="swarm-tenant",
            agent_id=agent_id,
            run_id="swarm-run-42",
            step_id=f"step-{idx}",
            model=req_model,
            estimated_input_tokens=in_tok,
            estimated_output_tokens=out_tok,
            timestamp=base_ts + idx,
        )
        print(
            f"[step-{idx}] agent={agent_id} req={req_model} -> "
            f"action={verdict.action} circuit={verdict.circuit_state.value} "
            f"assigned={verdict.assigned_model} est=${verdict.estimated_cost_usd:.4f} "
            f"saved=${verdict.estimated_savings_usd:.4f}"
        )
        if verdict.allowed and verdict.assigned_model is not None:
            engine.record_usage(
                tenant_id="swarm-tenant",
                agent_id=agent_id,
                run_id="swarm-run-42",
                step_id=f"step-{idx}",
                model=verdict.assigned_model,
                input_tokens=in_tok,
                output_tokens=out_tok,
                requested_model=req_model,
                downgraded=verdict.downgraded,
                timestamp=base_ts + idx + 0.1,
            )

    # Reconcile final external settlement reaching 100.0% hard cap, then verify OPEN_HALT
    engine.record_usage(
        tenant_id="swarm-tenant",
        agent_id="finalizer",
        run_id="swarm-run-42",
        step_id="step-6",
        model="gpt-4o",
        input_tokens=10_800,
        output_tokens=10_000,
        timestamp=base_ts + 7.0,
    )
    halted = engine.authorize_call(
        tenant_id="swarm-tenant",
        agent_id="finalizer",
        run_id="swarm-run-42",
        step_id="step-7",
        model="gpt-4o",
        estimated_input_tokens=5_000,
        estimated_output_tokens=2_000,
        timestamp=base_ts + 8.0,
    )
    print(
        f"[step-7] action={halted.action} allowed={halted.allowed} "
        f"circuit={halted.circuit_state.value} reason={halted.reason}"
    )
    print("\nSummary:", json.dumps(engine.get_tenant_summary("swarm-tenant", timestamp=base_ts + 9.0), indent=2))
    print("Ledger :", json.dumps(engine.verify_ledger(), indent=2))


if __name__ == "__main__":
    main()
