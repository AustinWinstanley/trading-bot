"""New read-only primitives for live debugging, beyond what the dashboard
exposes: ad hoc SELECT/WITH queries, raw state/reports file reads, config
text, and log tailing. Every function here is read-only in at least two
independent, verified ways — see each docstring for the specific pair —
mirroring dashboard/db.py's open_ro() (mode=ro SQLite connections refuse
writes at the VFS level regardless of any Python-level check here).
"""

from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
import time
from pathlib import Path

from dashboard import db as dashboard_db

_SELECT_LEADING_TOKEN = re.compile(r"[A-Za-z]+")
_DATE_RE = re.compile(r"^\d{8}$")


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


def _resolve_within(base_dir: Path, relative_path: str) -> Path:
    """Resolve relative_path against base_dir and guarantee the result
    stays inside base_dir. The upfront '..'/absolute-path reject is a
    clear-error convenience, not the actual guarantee — Path('/a') /
    '/etc/passwd' silently discards the left side entirely (a classic
    pathlib gotcha), so the is_relative_to() check after resolve() is
    what actually closes the escape, independent of the upfront check."""
    if relative_path.startswith(("/", "\\")) or ".." in Path(relative_path).parts:
        raise ValueError(
            f"path must be relative and stay inside the base directory: {relative_path!r}"
        )
    base = base_dir.resolve()
    candidate = (base / relative_path).resolve()
    if candidate != base and not candidate.is_relative_to(base):
        raise ValueError(f"path escapes {base_dir}: {relative_path!r}")
    return candidate


def safe_read_json(base_dir: Path, relative_path: str) -> dict:
    """Tolerant JSON read scoped under base_dir — missing file or invalid
    JSON both return a clear "not found"/"error" shape, matching
    dashboard_db.load_json's never-raises convention, never a traceback."""
    try:
        path = _resolve_within(base_dir, relative_path)
    except ValueError as exc:
        return {"exists": False, "path": relative_path, "error": str(exc)}
    if not path.is_file():
        return {"exists": False, "path": relative_path}
    try:
        return {"exists": True, "path": relative_path, "data": json.loads(path.read_text())}
    except json.JSONDecodeError as exc:
        return {"exists": True, "path": relative_path, "error": f"invalid JSON: {exc}"}


def safe_read_text(base_dir: Path, relative_path: str, max_bytes: int = 200_000) -> dict:
    """Same path guard as safe_read_json, for non-JSON files (e.g.
    reports/paper*/*.md daily notes). Truncates at max_bytes with a note
    rather than streaming an arbitrarily large file into a tool response."""
    try:
        path = _resolve_within(base_dir, relative_path)
    except ValueError as exc:
        return {"exists": False, "path": relative_path, "error": str(exc)}
    if not path.is_file():
        return {"exists": False, "path": relative_path}
    raw = path.read_bytes()
    truncated = len(raw) > max_bytes
    text = raw[:max_bytes].decode("utf-8", errors="replace")
    return {
        "exists": True,
        "path": relative_path,
        "text": text,
        "truncated": truncated,
        "size_bytes": len(raw),
    }


def list_dir(base_dir: Path, relative_path: str = "") -> dict:
    """Discovery helper: list files/dirs under base_dir/relative_path
    (non-recursive) so a caller can find what's available without
    guessing exact filenames."""
    try:
        path = _resolve_within(base_dir, relative_path) if relative_path else base_dir.resolve()
    except ValueError as exc:
        return {"exists": False, "path": relative_path, "error": str(exc)}
    if not path.is_dir():
        return {"exists": False, "path": relative_path}
    entries = []
    for child in sorted(path.iterdir()):
        try:
            stat = child.stat()
        except OSError:
            continue
        entries.append({
            "name": child.name,
            "is_dir": child.is_dir(),
            "size_bytes": None if child.is_dir() else stat.st_size,
            "modified_ts": dt.datetime.fromtimestamp(
                stat.st_mtime, tz=dt.timezone.utc
            ).isoformat(),
        })
    return {"exists": True, "path": relative_path, "entries": entries}


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


def tail_log(logs_dir: Path, date: str | None = None, lines: int = 200) -> dict:
    """Read logs/paper-<date>.log; date defaults to today in UTC, matching
    scripts/paper.sh's own `date -u +%Y%m%d` filename convention exactly
    (getting this wrong silently tails the wrong day). Missing file is a
    valid, expected state (mirrors the "no journal yet" tolerance
    throughout dashboard/db.py), not an error."""
    if date is None:
        date = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d")
    elif not _DATE_RE.match(date):
        raise ValueError(f"date must be YYYYMMDD, got {date!r}")

    filename = f"paper-{date}.log"
    try:
        path = _resolve_within(logs_dir, filename)
    except ValueError as exc:
        return {"exists": False, "date": date, "error": str(exc)}
    if not path.is_file():
        return {"exists": False, "date": date, "path": filename, "lines": [], "line_count": 0}

    all_lines = path.read_text(errors="replace").splitlines()
    tail = all_lines[-lines:] if lines > 0 else []
    return {
        "exists": True,
        "date": date,
        "path": filename,
        "lines": tail,
        "line_count": len(tail),
        "total_lines": len(all_lines),
    }
