"""Direct unit tests for mcp_server/debug.py's new read-only primitives —
no MCP protocol machinery involved, just the functions themselves.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from mcp_server import debug


@pytest.fixture
def ro_conn(tmp_path: Path):
    db_path = tmp_path / "t.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE t(a INTEGER, b TEXT)")
    conn.executemany("INSERT INTO t VALUES (?,?)", [(i, f"row{i}") for i in range(10)])
    conn.commit()
    conn.close()

    ro = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    ro.row_factory = sqlite3.Row
    yield ro
    ro.close()


class TestRunSelect:
    def test_plain_select(self, ro_conn):
        result = debug.run_select(ro_conn, "SELECT * FROM t WHERE a < 3")
        assert result["row_count"] == 3
        assert result["truncated"] is False
        assert result["columns"] == ["a", "b"]
        assert result["rows"][0] == {"a": 0, "b": "row0"}

    def test_with_cte(self, ro_conn):
        result = debug.run_select(
            ro_conn, "WITH x AS (SELECT * FROM t) SELECT * FROM x LIMIT 2"
        )
        assert result["row_count"] == 2

    def test_comment_prefixed_select_is_not_rejected(self, ro_conn):
        result = debug.run_select(ro_conn, "-- a comment\nSELECT * FROM t LIMIT 1")
        assert result["row_count"] == 1

    def test_max_rows_truncates_and_sets_flag(self, ro_conn):
        result = debug.run_select(ro_conn, "SELECT * FROM t", max_rows=3)
        assert result["row_count"] == 3
        assert result["truncated"] is True

    def test_under_cap_query_is_not_marked_truncated(self, ro_conn):
        result = debug.run_select(ro_conn, "SELECT * FROM t", max_rows=500)
        assert result["truncated"] is False

    @pytest.mark.parametrize("sql", [
        "INSERT INTO t VALUES (99, 'x')",
        "UPDATE t SET b = 'x' WHERE a = 0",
        "DELETE FROM t WHERE a = 0",
        "DROP TABLE t",
        "PRAGMA table_info(t)",
        "ATTACH DATABASE 'x' AS y",
        "EXPLAIN SELECT * FROM t",
        "VACUUM",
    ])
    def test_rejects_non_select_statements(self, ro_conn, sql):
        with pytest.raises(ValueError, match="only SELECT/WITH"):
            debug.run_select(ro_conn, sql)

    def test_rejects_chained_statements(self, ro_conn):
        with pytest.raises(ValueError):
            debug.run_select(ro_conn, "SELECT 1; DROP TABLE t")

    def test_query_exceeding_timeout_is_aborted(self, ro_conn):
        slow_sql = (
            "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c "
            "WHERE x < 100000000) SELECT count(*) FROM c"
        )
        start = time.monotonic()
        with pytest.raises(ValueError, match="timeout"):
            debug.run_select(ro_conn, slow_sql, timeout_seconds=0.2)
        assert time.monotonic() - start < 5.0

    def test_a_write_cannot_reach_disk_even_if_the_prefix_check_were_bypassed(self, ro_conn):
        """Defense in depth, made explicit: conn is already mode=ro, so
        even calling conn.execute() directly (bypassing run_select
        entirely) refuses to write — the prefix check is a second layer,
        not the only one."""
        with pytest.raises(sqlite3.OperationalError):
            ro_conn.execute("INSERT INTO t VALUES (999, 'z')")


class TestOpenQueryTarget:
    def test_paper_and_options_targets(self, tmp_path):
        from dashboard.db import ProfilePaths

        paper_db = tmp_path / "paper.db"
        options_db = tmp_path / "options.db"
        sqlite3.connect(paper_db).close()
        sqlite3.connect(options_db).close()
        paths = ProfilePaths(
            profile="base",
            config_path=tmp_path / "config.yaml",
            db_path=paper_db,
            risk_state_path=tmp_path / "risk_state.json",
            health_status_path=tmp_path / "health_status.json",
            options_db_path=options_db,
        )
        paper_conn = debug.open_query_target(paths, "paper")
        assert paper_conn is not None
        paper_conn.close()

        options_conn = debug.open_query_target(paths, "options")
        assert options_conn is not None
        options_conn.close()

        with pytest.raises(ValueError):
            debug.open_query_target(paths, "not-a-target")


class TestSafeReadJson:
    def test_reads_a_legitimate_nested_file(self, tmp_path):
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "nested.json").write_text('{"n": 1}')
        result = debug.safe_read_json(tmp_path, "sub/nested.json")
        assert result == {"exists": True, "path": "sub/nested.json", "data": {"n": 1}}

    def test_missing_file_is_not_an_error(self, tmp_path):
        result = debug.safe_read_json(tmp_path, "does_not_exist.json")
        assert result == {"exists": False, "path": "does_not_exist.json"}

    def test_invalid_json_returns_an_error_shape_not_a_raise(self, tmp_path):
        (tmp_path / "bad.json").write_text("{not valid json")
        result = debug.safe_read_json(tmp_path, "bad.json")
        assert result["exists"] is True
        assert "error" in result

    @pytest.mark.parametrize("escape", [
        "../secret.json",
        "../../etc/passwd",
        "/etc/passwd",
        "sub/../../secret.json",
    ])
    def test_rejects_path_traversal(self, tmp_path, escape):
        result = debug.safe_read_json(tmp_path, escape)
        assert result["exists"] is False
        assert "error" in result


class TestSafeReadText:
    def test_reads_and_truncates(self, tmp_path):
        (tmp_path / "big.md").write_text("x" * 100)
        result = debug.safe_read_text(tmp_path, "big.md", max_bytes=10)
        assert result["exists"] is True
        assert result["truncated"] is True
        assert len(result["text"]) == 10

    def test_rejects_path_traversal(self, tmp_path):
        result = debug.safe_read_text(tmp_path, "../../etc/passwd")
        assert result["exists"] is False


class TestListDir:
    def test_lists_files_and_dirs(self, tmp_path):
        (tmp_path / "a.json").write_text("{}")
        (tmp_path / "sub").mkdir()
        result = debug.list_dir(tmp_path)
        names = {e["name"] for e in result["entries"]}
        assert names == {"a.json", "sub"}

    def test_rejects_path_traversal(self, tmp_path):
        result = debug.list_dir(tmp_path, "../../etc")
        assert result["exists"] is False


class TestReadConfigRaw:
    def test_reads_known_profile(self, tmp_path):
        (tmp_path / "config.yaml").write_text("mode: paper\n# a comment\n")
        result = debug.read_config_raw(tmp_path, "base")
        assert result["exists"] is True
        assert result["text"] == "mode: paper\n# a comment\n"

    def test_missing_file_is_not_an_error(self, tmp_path):
        result = debug.read_config_raw(tmp_path, "2x")
        assert result == {"exists": False, "profile": "2x", "filename": "config_2x.yaml"}

    def test_unknown_profile_raises(self, tmp_path):
        with pytest.raises(ValueError, match="unknown profile"):
            debug.read_config_raw(tmp_path, "notaprofile")


class TestTailLog:
    def test_default_date_is_today_utc(self, tmp_path):
        import datetime as dt

        today = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d")
        (tmp_path / f"paper-{today}.log").write_text("\n".join(f"line{i}" for i in range(300)))
        result = debug.tail_log(tmp_path)
        assert result["date"] == today
        assert result["exists"] is True
        assert result["line_count"] == 200
        assert result["lines"][-1] == "line299"

    def test_missing_date_is_not_an_error(self, tmp_path):
        result = debug.tail_log(tmp_path, date="20000101")
        assert result == {
            "exists": False, "date": "20000101", "path": "paper-20000101.log",
            "lines": [], "line_count": 0,
        }

    def test_lines_parameter_is_respected(self, tmp_path):
        (tmp_path / "paper-20260101.log").write_text("\n".join(f"line{i}" for i in range(50)))
        result = debug.tail_log(tmp_path, date="20260101", lines=5)
        assert result["line_count"] == 5
        assert result["lines"] == [f"line{i}" for i in range(45, 50)]

    def test_rejects_malformed_date(self, tmp_path):
        with pytest.raises(ValueError, match="YYYYMMDD"):
            debug.tail_log(tmp_path, date="not-a-date")
