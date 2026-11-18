# Agent Budget Governor — Architecture

`agent-budget-governor` implements a deterministic, zero-cloud FinOps control plane that sits between AI agent orchestrators / MCP tool dispatchers and LLM inference providers. It enforces hard hierarchical USD and token ceilings in sub-millisecond local memory with zero external runtime dependencies.

## 5-Layer FinOps Governance Pipeline

1. **Deterministic Multi-Tier Pricing (`PricingCatalog`)**
   Calculates exact call cost across 5 billing dimensions:
   - Standard uncached input tokens (`input_per_1m_usd`)
   - Prompt-cached input tokens (`cached_input_per_1m_usd`)
   - Completion output tokens (`output_per_1m_usd`)
   - Extended thinking / reasoning tokens (`reasoning_per_1m_usd`)
   - Per-invocation tool execution flat fees (`tool_call_fee_usd`)
   Local execution tiers (`*-local`, `local/*`, e.g. `qwen3-30b-a3b-local`, `llama-3.3-70b-local`) always compute deterministically to `$0.00`.

2. **Hierarchical Envelope & Burn-Rate Velocity Enforcer (`HierarchicalLimiter`)**
   Maintains thread-safe cumulative USD and token counters across four nested scopes:
   - `STEP` (`(tenant_id, agent_id, run_id, step_id)`)
   - `RUN` (`(tenant_id, agent_id, run_id)`)
   - `AGENT` (`(tenant_id, agent_id)`)
   - `TENANT` (`(tenant_id,)`)
   Simultaneously maintains a sliding time window (`velocity_window_seconds`, default `60.0s`) per tenant to calculate real-time and projected USD/minute burn rate (`max_usd_per_minute`) and halt runaway agent retry loops before a billing spike materializes.

3. **Economic Circuit Breaker (`EconomicCircuitBreaker`)**
   Implements a three-state FinOps finite state machine per tenant:
   - `CLOSED` (`utilization < soft_limit_ratio`, default `< 0.80`): Authorizes frontier models directly.
   - `SOFT_DOWNGRADE` (`soft_limit_ratio <= utilization < hard_limit_ratio`): Automatically reroutes frontier calls (e.g., `gpt-4o -> qwen3-30b-a3b-local`, `claude-3-7-sonnet -> gemini-flash`, `gemini-2.5-pro -> gemini-flash`) and re-evaluates the downgraded cost against remaining caps so workflows finish cleanly without exceeding budget.
   - `OPEN_HALT` (`utilization >= hard_limit_ratio` or velocity spike): Hard-blocks further paid execution (`allowed=False`, `action="BLOCK"`) ensuring `0.0%` budget overrun.

4. **SHA-256 Hash-Chained Spend Ledger (`SpendLedger`)**
   Every `record_usage()` invocation appends a `SpendEvent` sealed into an immutable `BudgetReceipt` linked to `prev_hash` (starting from `0` × 64 genesis). Canonical JSON serialization ensures deterministic `entry_hash` and `receipt_sha256` verification across `verify_ledger()` and `export_jsonl()`. Any single-digit mutation in historical spend, tokens, or model IDs is caught immediately.

5. **MCP Bridge & Telemetry (`MCPBridgeServer`, `BudgetTelemetryEngine`, `BudgetHealthMonitor`)**
   Exposes `budget_authorize`, `budget_record`, `budget_summary`, and `budget_verify` over standard JSON-RPC 2.0 stdio with zero third-party packages.
