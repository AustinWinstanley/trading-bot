"""Read-only primitives for live debugging beyond what the dashboard
exposes: ad hoc SELECT/WITH queries and raw config text. Every function
here is read-only in at least two independent, verified ways — see each
docstring for the specific pair — mirroring dashboard/db.py's open_ro()
(mode=ro SQLite connections refuse writes at the VFS level regardless of
any Python-level check here).

The generic file primitives formerly defined here moved to
dashboard/files.py (2026-08) when the dashboard gained reports/ and
logs/ mounts of its own — re-exported below unchanged so every existing
import site (mcp_server/tools.py, tests) keeps working.
"""

from __future__ import annotations

import re
import sqlite3
import time
from pathlib import Path

from dashboard import db as dashboard_db
from dashboard.files import (  # noqa: F401  (re-exported; moved 2026-08)
    _resolve_within,
    list_dir,
    safe_read_json,
    safe_read_text,
    tail_log,
)

_SELECT_LEADING_TOKEN = re.compile(r"[A-Za-z]+")


def _strip_leading_comments(sql: str) -> str:
    """Strip leading whitespace/`-- line` and `/* block */` comments so a
    query that merely starts with a comment isn't rejected by the
    SELECT/WITH prefix check below."""
    s = sql
    while True:
        s = s.lstrip()
        if s.startswith("--"):
            newline = s.find("\n")
            s = s[newline + 1:] if newline != -1 else ""
            continue
        if s.startswith("/*"):
            end = s.find("*/")
            s = s[end + 2:] if end != -1 else ""
            continue
        return s


def open_query_target(paths: dashboard_db.ProfilePaths, target: str) -> sqlite3.Connection:
    """target in {"paper", "options"} -> paths.db_path / paths.options_db_path,
    opened via dashboard_db.open_ro — mode=ro, writes refused at the SQLite
    VFS level regardless of anything run_select() checks."""
    if target == "paper":
        path = paths.db_path
    elif target == "options":
        path = paths.options_db_path
    else:
        raise ValueError(f"target must be 'paper' or 'options', got {target!r}")
    return dashboard_db.open_ro(path)


def run_select(
    conn: sqlite3.Connection, sql: str, max_rows: int = 500, timeout_seconds: float = 5.0
) -> dict:
    """Execute a single read-only SELECT/WITH statement.

    Two independent guarantees, not one: (1) the connection itself is
    opened mode=ro (dashboard_db.open_ro), so SQLite refuses any write at
    the VFS level no matter what slips past the check here; (2)
    sqlite3.Cursor.execute() already refuses multi-statement input
    ("SELECT 1; DROP TABLE t" raises sqlite3.ProgrammingError: You can
    only execute one statement at a time — verified against the stdlib),
    so no manual statement-splitting is needed to block chaining. The
    prefix check below is a third, belt-and-suspenders layer: reject
    anything whose first real token isn't SELECT or WITH, by allowlist
    rather than by trying to enumerate every dangerous keyword.

    Returns {"columns": [...], "rows": [...], "row_count": int,
    "truncated": bool}. A conn.set_progress_handler() callback enforces
    timeout_seconds as a wall-clock cap independent of open_ro()'s
    connection-level lock-wait timeout.
    """
    body = _strip_leading_comments(sql)
    match = _SELECT_LEADING_TOKEN.match(body)
    first_word = match.group(0).upper() if match else ""
    if first_word not in {"SELECT", "WITH"}:
        raise ValueError(
            f"only SELECT/WITH statements are allowed, got: {sql.strip()[:80]!r}"
        )

    start = time.monotonic()

    def _watchdog() -> int:
        return 1 if (time.monotonic() - start) > timeout_seconds else 0

    conn.set_progress_handler(_watchdog, 1000)
    try:
        cursor = conn.execute(sql)
        columns = [d[0] for d in cursor.description] if cursor.description else []
        rows: list[dict] = []
        truncated = False
        for i, row in enumerate(cursor):
            if i >= max_rows:
                truncated = True
                break
            rows.append(dict(zip(columns, row)))
        return {
            "columns": columns,
            "rows": rows,
            "row_count": len(rows),
            "truncated": truncated,
        }
    except sqlite3.ProgrammingError as exc:
        # Includes sqlite3's own multi-statement rejection — see docstring.
        raise ValueError(str(exc)) from exc
    except sqlite3.OperationalError as exc:
        if "interrupted" in str(exc).lower():
            raise ValueError(f"query exceeded {timeout_seconds}s timeout") from exc
        raise
    finally:
        conn.set_progress_handler(None, 0)


def read_config_raw(repo_root: Path, profile: str) -> dict:
    """Raw YAML text (not parsed) of config.yaml/config_2x.yaml — comments
    included, exactly what's on disk, via the same dashboard_db.PROFILES
    mapping the rest of the read-only layer uses for profile resolution."""
    entry = dashboard_db.PROFILES.get(profile)
    if entry is None:
        raise ValueError(
            f"unknown profile {profile!r}; expected one of {sorted(dashboard_db.PROFILES)}"
        )
    filename, _ = entry
    path = repo_root / filename
    if not path.is_file():
        return {"exists": False, "profile": profile, "filename": filename}
    return {
        "exists": True,
        "profile": profile,
        "filename": filename,
        "text": path.read_text(),
    }
