"""Tests for MCP bridge, telemetry, health monitor, doctor, matrix, and CLI."""

from __future__ import annotations

import io
import json

from agent_budget_governor.cli import main as cli_main
from agent_budget_governor.core.engine import BudgetGovernorEngine
from agent_budget_governor.core.models import BudgetPolicy
from agent_budget_governor.serve.health_monitor import BudgetHealthMonitor
from agent_budget_governor.serve.mcp_bridge import MCPBridgeServer
from agent_budget_governor.serve.telemetry import BudgetTelemetryEngine
from agent_budget_governor.tools.bench_matrix import BudgetBenchMatrixRunner
from agent_budget_governor.tools.doctor import run_doctor


def test_mcp_bridge_jsonrpc_lifecycle_and_all_four_tools() -> None:
    telemetry = BudgetTelemetryEngine()
    engine = BudgetGovernorEngine(
        policy=BudgetPolicy(per_step_usd_cap=0.50, per_tenant_usd_cap=1.00),
        telemetry=telemetry,
    )
    server = MCPBridgeServer(engine=engine, telemetry=telemetry)

    init_resp = server.handle_request({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert init_resp is not None
    assert init_resp["result"]["serverInfo"]["name"] == "agent-budget-governor"

    assert server.handle_request({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None

    tools_resp = server.handle_request({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert tools_resp is not None
    tool_names = [t["name"] for t in tools_resp["result"]["tools"]]
    assert tool_names == ["budget_authorize", "budget_record", "budget_summary", "budget_verify"]

    # Call budget_authorize
    auth_resp = server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "budget_authorize",
                "arguments": {
                    "tenant_id": "mcp-tenant",
                    "model": "gpt-4o",
                    "estimated_input_tokens": 10_000,
                    "estimated_output_tokens": 2_000,
                },
            },
        }
    )
    assert auth_resp is not None
    auth_data = json.loads(auth_resp["result"]["content"][0]["text"])
    assert auth_data["allowed"] is True
    assert auth_data["circuit_state"] == "CLOSED"

    # Call budget_record
    rec_resp = server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {
                "name": "budget_record",
                "arguments": {
                    "tenant_id": "mcp-tenant",
                    "model": "gpt-4o",
                    "input_tokens": 10_000,
                    "output_tokens": 2_000,
                },
            },
        }
    )
    assert rec_resp is not None
    rec_data = json.loads(rec_resp["result"]["content"][0]["text"])
    assert rec_data["receipt_id"] == "BR-000001"
    assert len(rec_data["receipt_sha256"]) == 64

    # Call budget_summary
    sum_resp = server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {"name": "budget_summary", "arguments": {"tenant_id": "mcp-tenant"}},
        }
    )
    assert sum_resp is not None
    sum_data = json.loads(sum_resp["result"]["content"][0]["text"])
    assert sum_data["tenant_id"] == "mcp-tenant"
    assert sum_data["spend_usd"] > 0

    # Call budget_verify
    ver_resp = server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 6,
            "method": "tools/call",
            "params": {"name": "budget_verify", "arguments": {}},
        }
    )
    assert ver_resp is not None
    ver_data = json.loads(ver_resp["result"]["content"][0]["text"])
    assert ver_data["valid"] is True
    assert ver_data["checked_count"] == 1


def test_mcp_stdio_stream_error_handling() -> None:
    server = MCPBridgeServer()
    stdin = io.StringIO(
        "not-valid-json\n"
        + json.dumps({"jsonrpc": "2.0", "id": 10, "method": "ping"})
        + "\n"
        + json.dumps({"jsonrpc": "2.0", "id": 11, "method": "unknown/method"})
        + "\n"
    )
    stdout = io.StringIO()
    server.serve_stdio(stdin=stdin, stdout=stdout)
    lines = [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]
    assert lines[0]["error"]["code"] == -32700
    assert lines[1]["result"] == {}
    assert lines[2]["error"]["code"] == -32601


def test_health_monitor_doctor_and_bench_matrix() -> None:
    health = BudgetHealthMonitor().check_health()
    assert health["status"] == "healthy"
    assert health["probe_passed"] is True
    assert health["zero_third_party_deps"] is True

    doc = run_doctor()
    assert doc["ok"] is True
    assert doc["passed_count"] == doc["total_count"] == 5

    matrix = BudgetBenchMatrixRunner().run(iterations=10)
    assert matrix["gate_accuracy_pct"] == 100.0
    assert matrix["overrun_rate_pct"] == 0.0
    assert matrix["total_evaluations"] == 100


def test_cli_subcommands_json(capsys) -> None:
    assert cli_main(["doctor", "--json"]) == 0
    doc_out = json.loads(capsys.readouterr().out)
    assert doc_out["ok"] is True

    assert cli_main(["matrix", "--iterations", "5", "--json"]) == 0
    mat_out = json.loads(capsys.readouterr().out)
    assert mat_out["overrun_rate_pct"] == 0.0

    assert cli_main(["authorize", "--tenant", "cli-t", "--model", "gpt-4o", "--spent-usd", "22.0", "--tenant-cap", "25.0", "--json"]) == 0
    auth_out = json.loads(capsys.readouterr().out)
    assert auth_out["circuit_state"] == "SOFT_DOWNGRADE"
    assert auth_out["assigned_model"] == "qwen3-30b-a3b-local"

    assert cli_main(["simulate", "--steps", "8", "--tenant-cap", "1.0", "--json"]) == 0
    sim_out = json.loads(capsys.readouterr().out)
    assert sim_out["ledger"]["valid"] is True
    assert sim_out["summary"]["spend_usd"] <= 1.0


def test_mcp_invalid_arguments_and_telemetry_snapshot() -> None:
    telemetry = BudgetTelemetryEngine()
    engine = BudgetGovernorEngine(telemetry=telemetry)
    server = MCPBridgeServer(engine=engine, telemetry=telemetry)

    bad_call = server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 99,
            "method": "tools/call",
            "params": {"name": "budget_authorize", "arguments": {"tenant_id": "t1"}},
        }
    )
    assert bad_call is not None
    assert bad_call["error"]["code"] == -32602

    unknown_tool = server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 100,
            "method": "tools/call",
            "params": {"name": "nonexistent_tool", "arguments": {}},
        }
    )
    assert unknown_tool is not None
    assert unknown_tool["error"]["code"] == -32602

    engine.authorize_call(
        tenant_id="tel-t",
        agent_id="a1",
        run_id="r1",
        step_id="s1",
        model="gpt-4o",
        estimated_input_tokens=4_000,
        estimated_output_tokens=1_000,
    )
    engine.record_usage(
        tenant_id="tel-t",
        agent_id="a1",
        run_id="r1",
        step_id="s1",
        model="gpt-4o",
        input_tokens=4_000,
        output_tokens=1_000,
    )
    snap = telemetry.to_dict()
    assert snap["total_authorizations"] == 1
    assert snap["total_recorded_events"] == 1
    assert snap["total_spend_usd"] > 0

