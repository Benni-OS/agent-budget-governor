"""Quickstart example demonstrating BudgetGovernorEngine authorization, usage recording, and SHA-256 verification."""

from __future__ import annotations

import json

from agent_budget_governor import BudgetGovernorEngine, BudgetPolicy


def main() -> None:
    policy = BudgetPolicy(
        policy_id="quickstart-policy",
        per_step_usd_cap=0.25,
        per_run_usd_cap=2.00,
        per_agent_usd_cap=5.00,
        per_tenant_usd_cap=10.00,
        soft_limit_ratio=0.80,
        hard_limit_ratio=1.00,
    )
    engine = BudgetGovernorEngine(policy=policy)

    # 1. Pre-flight authorization
    verdict = engine.authorize_call(
        tenant_id="acme-corp",
        agent_id="research-agent",
        run_id="run-001",
        step_id="step-1",
        model="gpt-4o",
        estimated_input_tokens=12_000,
        estimated_cached_tokens=4_000,
        estimated_output_tokens=3_000,
        estimated_tool_calls=2,
    )
    print(f"Verdict ID     : {verdict.verdict_id}")
    print(f"Allowed        : {verdict.allowed} ({verdict.action})")
    print(f"Circuit State  : {verdict.circuit_state.value}")
    print(f"Assigned Model : {verdict.assigned_model}")
    print(f"Est. Cost USD  : ${verdict.estimated_cost_usd:.6f}")
    print(f"Latency        : {verdict.latency_ms:.4f} ms")

    # 2. Record actual completion spend
    if verdict.allowed and verdict.assigned_model is not None:
        receipt = engine.record_usage(
            tenant_id="acme-corp",
            agent_id="research-agent",
            run_id="run-001",
            step_id="step-1",
            model=verdict.assigned_model,
            input_tokens=11_500,
            cached_input_tokens=4_000,
            output_tokens=2_600,
            tool_calls=2,
            requested_model=verdict.requested_model,
            downgraded=verdict.downgraded,
        )
        print(f"\nReceipt ID     : {receipt.receipt_id}")
        print(f"Recorded Cost  : ${receipt.cost_usd:.6f}")
        print(f"Receipt SHA256 : {receipt.receipt_sha256}")

    # 3. Tenant FinOps summary & ledger integrity verification
    print("\nTenant Summary:")
    print(json.dumps(engine.get_tenant_summary("acme-corp"), indent=2))
    print("\nLedger Verification:")
    print(json.dumps(engine.verify_ledger(), indent=2))


if __name__ == "__main__":
    main()
