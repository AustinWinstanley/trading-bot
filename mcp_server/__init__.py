"""Read-only MCP debug server for live, in-session server access.

Like dashboard/ (see dashboard/__init__.py), this package never imports
engine.execute, engine.data, scripts.run_daily, or scripts.healthcheck —
those transitively touch the Alpaca client. It reuses dashboard.db's
*_payload functions and read-only SQLite/JSON helpers for the same
capabilities the dashboard exposes, and adds a few new read-only-only
primitives in mcp_server/debug.py (ad hoc SELECT queries, raw state/
config/reports file reads, log tailing) — all layered on top of
dashboard.db.open_ro()'s mode=ro SQLite connections and path-traversal
guards, never on direct filesystem writes or Alpaca calls. This package
also never imports Flask — see mcp_server/requirements.txt.
"""
