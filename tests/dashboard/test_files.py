"""Ownership tests for dashboard/files.py — the guarded file readers
moved here from mcp_server/debug.py (which re-exports them; its own
test_debug.py keeps running against the shim as a regression guard on
the re-export).
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from dashboard import files


class TestResolveWithin:
    @pytest.mark.parametrize("escape", [
        "../secret.json",
        "../../etc/passwd",
        "/etc/passwd",
        "sub/../../secret.json",
    ])
    def test_rejects_traversal(self, tmp_path: Path, escape):
        with pytest.raises(ValueError):
            files._resolve_within(tmp_path, escape)

    def test_accepts_nested_path(self, tmp_path: Path):
        (tmp_path / "sub").mkdir()
        resolved = files._resolve_within(tmp_path, "sub/file.json")
        assert resolved == (tmp_path / "sub" / "file.json").resolve()


class TestSafeReadJson:
    def test_reads_and_tolerates_missing(self, tmp_path: Path):
        (tmp_path / "a.json").write_text('{"n": 1}')
        assert files.safe_read_json(tmp_path, "a.json")["data"] == {"n": 1}
        assert files.safe_read_json(tmp_path, "missing.json")["exists"] is False

    def test_traversal_returns_error_shape_not_raise(self, tmp_path: Path):
        result = files.safe_read_json(tmp_path, "../x.json")
        assert result["exists"] is False and "error" in result


class TestSafeReadText:
    def test_truncates(self, tmp_path: Path):
        (tmp_path / "big.md").write_text("x" * 100)
        result = files.safe_read_text(tmp_path, "big.md", max_bytes=10)
        assert result["truncated"] is True and len(result["text"]) == 10


class TestTailLog:
    def test_utc_date_convention_and_tail(self, tmp_path: Path):
        today = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d")
        (tmp_path / f"paper-{today}.log").write_text("\n".join(f"l{i}" for i in range(10)))
        result = files.tail_log(tmp_path, lines=3)
        assert result["date"] == today
        assert result["lines"] == ["l7", "l8", "l9"]

    def test_missing_file_is_quiet(self, tmp_path: Path):
        assert files.tail_log(tmp_path, date="20000101")["exists"] is False

    def test_rejects_malformed_date(self, tmp_path: Path):
        with pytest.raises(ValueError):
            files.tail_log(tmp_path, date="nope")
