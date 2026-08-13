"""Server factory. Mirrors dashboard/app.py::create_app's explicit
repo_root parameter so tests can point this at a tmp fixture directory
instead of the real repo checkout.
"""

from __future__ import annotations

from pathlib import Path

from mcp.server.mcpserver import MCPServer

from .tools import register_tools

_PACKAGE_ROOT = Path(__file__).resolve().parent


def create_server(repo_root: Path | str | None = None) -> MCPServer:
    if repo_root is None:
        repo_root = _PACKAGE_ROOT.parent
    repo_root = Path(repo_root)

    mcp = MCPServer(
        name="trading-bot-debug",
        instructions=(
            "Read-only live access to a paper-trading server's 'base' and "
            "'2x' profiles: journal databases, risk/health state, config, "
            "research reports, and cron logs. Every tool here is "
            "structurally read-only (SQLite connections opened mode=ro; "
            "query_database accepts only SELECT/WITH statements; file "
            "reads are path-traversal-guarded to state/ or reports/). "
            "Nothing here can place an order, touch a broker, or write to "
            "the journal."
        ),
    )
    register_tools(mcp, repo_root)
    return mcp
