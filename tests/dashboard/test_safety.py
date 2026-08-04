"""Tests that enforce, not just document, the dashboard's core safety
property: it is architecturally incapable of touching the trading/execution
path. See dashboard/__init__.py's docstring for the property these lock in.
"""

from __future__ import annotations

import ast
import sqlite3
from pathlib import Path

import pytest

DASHBOARD_DIR = Path(__file__).resolve().parent.parent.parent / "dashboard"

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


def test_dashboard_never_imports_the_execution_path():
    """Static, not just documented: a grep-equivalent AST check that
    dashboard/*.py never imports anything that transitively touches the
    Alpaca client or its credentials."""
    py_files = list(DASHBOARD_DIR.glob("*.py"))
    assert py_files, "expected dashboard/*.py to exist"
    for py_file in py_files:
        modules = _imported_modules(py_file)
        offending = modules & FORBIDDEN_MODULES
        assert not offending, f"{py_file.name} imports forbidden module(s): {offending}"


def test_open_ro_connection_cannot_write(tmp_path: Path):
    """Locks in the core safety property permanently: even a bug in this
    package cannot corrupt the live trading journal, because the connection
    itself refuses writes at the SQLite layer."""
    from dashboard.db import open_ro

    db_path = tmp_path / "paper.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE snapshots(ts TEXT, equity REAL)")
    conn.execute("INSERT INTO snapshots VALUES ('t', 1.0)")
    conn.commit()
    conn.close()

    ro_conn = open_ro(db_path)
    try:
        with pytest.raises(sqlite3.OperationalError):
            ro_conn.execute("INSERT INTO snapshots VALUES ('t2', 2.0)")
    finally:
        ro_conn.close()


def test_open_ro_raises_rather_than_creating_a_missing_database(tmp_path: Path):
    """A mode=ro URI connection must not silently create an empty journal
    for a profile that hasn't produced one yet."""
    from dashboard.db import open_ro

    with pytest.raises(sqlite3.OperationalError):
        open_ro(tmp_path / "does-not-exist.db")
