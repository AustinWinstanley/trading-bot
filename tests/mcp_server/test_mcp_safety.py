"""Tests that enforce, not just document, mcp_server's core safety
property: it is architecturally incapable of touching the trading/
execution path, and (unlike dashboard/) carries no Flask dependency at
all. See mcp_server/__init__.py's docstring for the properties these lock
in.
"""

from __future__ import annotations

import ast
from pathlib import Path

MCP_SERVER_DIR = Path(__file__).resolve().parent.parent.parent / "mcp_server"

FORBIDDEN_MODULES = {
    "engine.execute",
    "engine.data",
    "scripts.run_daily",
    "scripts.healthcheck",
}


def _imported_modules(py_file: Path) -> set[str]:
    tree = ast.parse(py_file.read_text(), filename=str(py_file))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def test_mcp_server_never_imports_the_execution_path():
    """Static, not just documented: a grep-equivalent AST check that
    mcp_server/*.py never imports anything that transitively touches the
    Alpaca client or its credentials."""
    py_files = list(MCP_SERVER_DIR.glob("*.py"))
    assert py_files, "expected mcp_server/*.py to exist"
    for py_file in py_files:
        modules = _imported_modules(py_file)
        offending = modules & FORBIDDEN_MODULES
        assert not offending, f"{py_file.name} imports forbidden module(s): {offending}"


def test_mcp_server_never_imports_dashboard_routes_or_app():
    """mcp_server reuses dashboard.db (the pure read-only data layer) but
    must never import dashboard.routes or dashboard.app — those pull in
    Flask, which mcp_server/Dockerfile deliberately never installs (see
    mcp_server/requirements.txt and the next test)."""
    py_files = list(MCP_SERVER_DIR.glob("*.py"))
    for py_file in py_files:
        modules = _imported_modules(py_file)
        offending = {m for m in modules if m in {"dashboard.routes", "dashboard.app"}}
        assert not offending, f"{py_file.name} imports forbidden module(s): {offending}"


def test_mcp_server_has_no_flask_dependency():
    """Locks in the deliberate design choice, not just requirements.txt's
    absence of it: nothing in mcp_server/ imports flask, directly or via
    a submodule import."""
    py_files = list(MCP_SERVER_DIR.glob("*.py"))
    for py_file in py_files:
        modules = _imported_modules(py_file)
        offending = {m for m in modules if m == "flask" or m.startswith("flask.")}
        assert not offending, f"{py_file.name} imports flask: {offending}"
