"""Module entrypoint (`python -m agent_budget_governor.serve`) for stdio MCP server."""

from __future__ import annotations

from agent_budget_governor.serve.mcp_bridge import MCPBridgeServer


def main() -> None:
    MCPBridgeServer().serve_stdio()


if __name__ == "__main__":
    main()
