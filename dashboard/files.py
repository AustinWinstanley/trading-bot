"""Guarded read-only file access, shared by dashboard/ and mcp_server/.

Moved here from mcp_server/debug.py (2026-08) when the dashboard gained
reports/ and logs/ mounts of its own — the dependency direction is
mcp_server -> dashboard, never the reverse, so shared primitives live on
this side (mcp_server/debug.py re-exports them unchanged). Everything
here is stdlib-only and path-traversal-guarded; nothing writes.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path

_DATE_RE = re.compile(r"^\d{8}$")


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
    dashboard.db.load_json's never-raises convention, never a traceback."""
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
    rather than streaming an arbitrarily large file into a response."""
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
