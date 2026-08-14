"""Integration-level tests: exercise the actual @mcp.tool()-registered
functions through MCPServer.call_tool() (in-process, no HTTP transport —
call_tool() is a public method the server itself uses internally, see
mcp/server/mcpserver/server.py's _handle_call_tool). Errors raised inside
a tool surface here as mcp.server.mcpserver.exceptions.ToolError (the
outer try/except that turns them into a CallToolResult(is_error=True) for
network clients only wraps the transport-facing _handle_call_tool, not
call_tool() itself — verified against the installed SDK).
"""

from __future__ import annotations

import asyncio
import json

import pytest
from mcp.server.mcpserver.exceptions import ToolError


def _call(mcp_server, name: str, arguments: dict) -> dict:
    result = asyncio.run(mcp_server.call_tool(name, arguments))
    assert result.is_error is False, result.content[0].text
    return json.loads(result.content[0].text)


def test_all_expected_tools_are_registered(mcp_server):
    tools = asyncio.run(mcp_server.list_tools())
    names = {t.name for t in tools}
    assert names == {
        "get_summary", "get_equity_curve", "get_orders", "get_positions",
        "get_exposure", "get_rejections", "get_options", "get_trends",
        "get_round_trips", "query_database", "read_state_file",
        "list_state_files", "read_config", "read_report", "list_reports",
        "tail_trading_log",
    }


def test_get_summary_returns_real_data(mcp_server):
    data = _call(mcp_server, "get_summary", {"profile": "base"})
    assert data["profile"] == "base"
    assert data["equity"] == 10500.0


def test_get_summary_unknown_profile_raises(mcp_server):
    with pytest.raises(ToolError, match="unknown profile"):
        asyncio.run(mcp_server.call_tool("get_summary", {"profile": "bogus"}))


def test_get_positions(mcp_server):
    data = _call(mcp_server, "get_positions", {"profile": "base"})
    symbols = {p["symbol"] for p in data["positions"]}
    assert "SPY" in symbols


def test_get_orders(mcp_server):
    data = _call(mcp_server, "get_orders", {"profile": "base"})
    assert len(data["orders"]) == 2


def test_get_equity_curve(mcp_server):
    data = _call(mcp_server, "get_equity_curve", {"profile": "base", "days": 30})
    assert len(data["points"]) >= 1


def test_get_exposure(mcp_server):
    data = _call(mcp_server, "get_exposure", {"profile": "base"})
    assert "latest_exposure" in data


def test_get_rejections(mcp_server):
    data = _call(mcp_server, "get_rejections", {"profile": "base"})
    assert "top_reasons" in data
    assert "by_sleeve_side" in data


def test_get_options_missing_db_returns_empty_shape(mcp_server):
    data = _call(mcp_server, "get_options", {"profile": "base"})
    assert data == {"structures": [], "reconciliation_events": []}


def test_query_database_legit_select(mcp_server):
    data = _call(mcp_server, "query_database", {
        "profile": "base", "target": "paper", "sql": "SELECT count(*) AS n FROM orders",
    })
    assert data["rows"] == [{"n": 2}]


def test_query_database_rejects_mutation(mcp_server):
    with pytest.raises(ToolError, match="only SELECT/WITH"):
        asyncio.run(mcp_server.call_tool("query_database", {
            "profile": "base", "target": "paper", "sql": "DELETE FROM orders",
        }))


def test_read_state_file(mcp_server):
    data = _call(mcp_server, "read_state_file", {"relative_path": "risk_state.json"})
    assert data["exists"] is True
    assert data["data"]["peak_equity"] == 10500.0


def test_read_state_file_path_traversal_is_blocked(mcp_server):
    data = _call(mcp_server, "read_state_file", {"relative_path": "../config.yaml"})
    assert data["exists"] is False
    assert "error" in data


def test_list_state_files(mcp_server):
    data = _call(mcp_server, "list_state_files", {})
    names = {e["name"] for e in data["entries"]}
    assert "risk_state.json" in names


def test_read_config(mcp_server):
    data = _call(mcp_server, "read_config", {"profile": "2x"})
    assert data["exists"] is True
    assert "mode" in data["text"]


def test_read_report_json(mcp_server):
    data = _call(mcp_server, "read_report", {"relative_path": "sample_study.json"})
    assert data["exists"] is True
    assert data["data"]["verdict"] == "reject"


def test_read_report_markdown(mcp_server):
    data = _call(mcp_server, "read_report", {"relative_path": "paper/2026-08-13.md"})
    assert data["exists"] is True
    assert "test fixture note" in data["text"]


def test_list_reports(mcp_server):
    data = _call(mcp_server, "list_reports", {})
    names = {e["name"] for e in data["entries"]}
    assert "experiments" in names
    assert "sample_study.json" in names


def test_tail_trading_log(mcp_server):
    data = _call(mcp_server, "tail_trading_log", {"lines": 10})
    assert data["exists"] is True
    assert any("job=daily" in line for line in data["lines"])
