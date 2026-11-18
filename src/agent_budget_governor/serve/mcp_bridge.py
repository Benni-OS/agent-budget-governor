"""Zero-dependency JSON-RPC 2.0 stdio MCP server for Agent Budget Governor."""

from __future__ import annotations

import json
import sys
from typing import Any, TextIO

from agent_budget_governor.core.engine import BudgetGovernorEngine
from agent_budget_governor.core.pricing import UnknownModelError
from agent_budget_governor.serve.telemetry import BudgetTelemetryEngine

SERVER_NAME = "agent-budget-governor"
SERVER_VERSION = "0.1.0"
PROTOCOL_VERSION = "2024-11-05"

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "budget_authorize",
        "description": (
            "Evaluate pre-flight USD/token budget authorization across step, run, agent, "
            "and tenant envelopes with burn-rate velocity protection and automatic "
            "frontier-to-local circuit breaker downgrade."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "tenant_id": {"type": "string", "description": "Tenant or customer identifier"},
                "agent_id": {"type": "string", "description": "Agent identifier", "default": "default-agent"},
                "run_id": {"type": "string", "description": "Workflow run identifier", "default": "default-run"},
                "step_id": {"type": "string", "description": "Step identifier", "default": "step-1"},
                "model": {"type": "string", "description": "Requested model identifier"},
                "estimated_input_tokens": {"type": "integer", "default": 0},
                "estimated_cached_tokens": {"type": "integer", "default": 0},
                "estimated_output_tokens": {"type": "integer", "default": 0},
                "estimated_reasoning_tokens": {"type": "integer", "default": 0},
                "estimated_tool_calls": {"type": "integer", "default": 0},
            },
            "required": ["tenant_id", "model"],
        },
    },
    {
        "name": "budget_record",
        "description": (
            "Record actual token and tool call usage into hierarchical budget envelopes "
            "and append a SHA-256 hash-chained BudgetReceipt to the spend ledger."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "tenant_id": {"type": "string", "description": "Tenant or customer identifier"},
                "agent_id": {"type": "string", "default": "default-agent"},
                "run_id": {"type": "string", "default": "default-run"},
                "step_id": {"type": "string", "default": "step-1"},
                "model": {"type": "string", "description": "Executed model identifier"},
                "input_tokens": {"type": "integer", "default": 0},
                "cached_input_tokens": {"type": "integer", "default": 0},
                "output_tokens": {"type": "integer", "default": 0},
                "reasoning_tokens": {"type": "integer", "default": 0},
                "tool_calls": {"type": "integer", "default": 0},
                "requested_model": {"type": "string"},
                "downgraded": {"type": "boolean", "default": False},
            },
            "required": ["tenant_id", "model"],
        },
    },
    {
        "name": "budget_summary",
        "description": (
            "Retrieve real-time spend, remaining USD/token quota, burn velocity (USD/min), "
            "downgrade savings, and circuit breaker state for a tenant."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "tenant_id": {"type": "string", "description": "Tenant identifier to inspect"},
            },
            "required": ["tenant_id"],
        },
    },
    {
        "name": "budget_verify",
        "description": (
            "Cryptographically verify sequence continuity and SHA-256 hash-chain integrity "
            "across the entire spend attribution ledger."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
]


class MCPBridgeServer:
    """Synchronous JSON-RPC 2.0 stdio MCP bridge exposing FinOps governor tools."""

    def __init__(
        self,
        engine: BudgetGovernorEngine | None = None,
        telemetry: BudgetTelemetryEngine | None = None,
    ) -> None:
        self.telemetry = telemetry or BudgetTelemetryEngine()
        self.engine = engine or BudgetGovernorEngine(telemetry=self.telemetry)

    def handle_request(self, request: dict[str, Any]) -> dict[str, Any] | None:
        req_id = request.get("id")
        method = request.get("method", "")
        params = request.get("params") or {}

        if method == "initialize":
            return self._ok(
                req_id,
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                },
            )

        if method in ("notifications/initialized", "initialized"):
            return None

        if method == "ping":
            return self._ok(req_id, {})

        if method == "tools/list":
            return self._ok(req_id, {"tools": TOOL_DEFINITIONS})

        if method == "tools/call":
            return self._handle_tool_call(req_id, params)

        if req_id is not None:
            return self._err(req_id, -32601, f"Method not found: {method}")
        return None

    def _handle_tool_call(self, req_id: Any, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name", "")
        args = params.get("arguments") or {}

        try:
            if name == "budget_authorize":
                tenant_id, model = self._require_tenant_and_model(args)
                result_obj = self.engine.authorize_call(
                    tenant_id=tenant_id,
                    agent_id=str(args.get("agent_id", "default-agent")),
                    run_id=str(args.get("run_id", "default-run")),
                    step_id=str(args.get("step_id", "step-1")),
                    model=model,
                    estimated_input_tokens=int(args.get("estimated_input_tokens", 0)),
                    estimated_cached_tokens=int(args.get("estimated_cached_tokens", 0)),
                    estimated_output_tokens=int(args.get("estimated_output_tokens", 0)),
                    estimated_reasoning_tokens=int(args.get("estimated_reasoning_tokens", 0)),
                    estimated_tool_calls=int(args.get("estimated_tool_calls", 0)),
                ).to_dict()
            elif name == "budget_record":
                tenant_id, model = self._require_tenant_and_model(args)
                req_model = args.get("requested_model")
                result_obj = self.engine.record_usage(
                    tenant_id=tenant_id,
                    agent_id=str(args.get("agent_id", "default-agent")),
                    run_id=str(args.get("run_id", "default-run")),
                    step_id=str(args.get("step_id", "step-1")),
                    model=model,
                    input_tokens=int(args.get("input_tokens", 0)),
                    cached_input_tokens=int(args.get("cached_input_tokens", 0)),
                    output_tokens=int(args.get("output_tokens", 0)),
                    reasoning_tokens=int(args.get("reasoning_tokens", 0)),
                    tool_calls=int(args.get("tool_calls", 0)),
                    requested_model=str(req_model) if req_model is not None else None,
                    downgraded=bool(args.get("downgraded", False)),
                ).to_dict()
            elif name == "budget_summary":
                tenant_id = args.get("tenant_id")
                if not isinstance(tenant_id, str) or not tenant_id:
                    return self._err(req_id, -32602, "Missing required string argument: tenant_id")
                result_obj = self.engine.get_tenant_summary(tenant_id)
            elif name == "budget_verify":
                result_obj = self.engine.verify_ledger()
            else:
                return self._err(req_id, -32602, f"Unknown tool: {name}")
        except (UnknownModelError, ValueError, TypeError) as exc:
            return self._err(req_id, -32602, f"Invalid tool arguments: {exc}")

        return self._ok(
            req_id,
            {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(result_obj, indent=2),
                    }
                ],
                "isError": False,
            },
        )

    @staticmethod
    def _require_tenant_and_model(args: dict[str, Any]) -> tuple[str, str]:
        tenant_id = args.get("tenant_id")
        model = args.get("model")
        if not isinstance(tenant_id, str) or not tenant_id:
            raise ValueError("Missing required non-empty string argument: tenant_id")
        if not isinstance(model, str) or not model:
            raise ValueError("Missing required non-empty string argument: model")
        return tenant_id, model

    def serve_stdio(
        self,
        stdin: TextIO | None = None,
        stdout: TextIO | None = None,
    ) -> None:
        in_stream = stdin or sys.stdin
        out_stream = stdout or sys.stdout
        for raw_line in in_stream:
            line = raw_line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError as exc:
                resp = self._err(None, -32700, f"Parse error: {exc}")
                out_stream.write(json.dumps(resp) + "\n")
                out_stream.flush()
                continue

            resp = self.handle_request(msg)
            if resp is not None:
                out_stream.write(json.dumps(resp) + "\n")
                out_stream.flush()

    @staticmethod
    def _ok(req_id: Any, result: dict[str, Any]) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    @staticmethod
    def _err(req_id: Any, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}
