"""Tests for the research/notes endpoints and the Phase-3 attention
completions (shadow staleness, halt reason, CRITICAL log lines).
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from dashboard import db as dashboard_db

NOW = dt.datetime(2026, 8, 14, 15, 0, tzinfo=dt.timezone.utc)


def test_research_route_seeded_collector_and_review_bar(client):
    data = client.get("/api/base/research").get_json()
    collectors = data["collectors"]
    assert set(collectors) == {
        "options_shadow", "momentum_options_shadow",
        "event_volatility_shadow", "zero_dte_shadow",
    }
    assert collectors["options_shadow"]["exists"] is True
    assert collectors["options_shadow"]["observation_count"] == 1
    assert collectors["options_shadow"]["stale"] is False
    assert "raw" not in (collectors["options_shadow"]["latest"] or {})
    # The three absent collectors are rendered, not omitted.
    assert collectors["zero_dte_shadow"]["exists"] is False

    experiments = {e["name"]: e for e in data["experiments"]}
    bar = experiments["bull_put_delta_selected_live"]
    assert bar["minimum_observations"] == 20
    assert bar["observed"] == 1
    assert bar["minimum_expirations"] == 3
    assert bar["expirations_observed"] == 1


def test_research_route_2x_all_absent_is_shape_complete(client):
    data = client.get("/api/2x/research").get_json()
    assert all(not c["exists"] for c in data["collectors"].values())


def test_notes_route_lists_and_reads_latest(client):
    data = client.get("/api/base/notes").get_json()
    assert data["available"] == ["2026-08-12.md"]
    assert data["note"]["exists"] is True
    assert "Paper run 2026-08-12" in data["note"]["text"]


def test_notes_route_by_name_rejects_invalid_names(client):
    for bad in ("../../.env", "..%2F..%2F.env", "x.json", "notes.txt"):
        resp = client.get(f"/api/base/notes/{bad}")
        # Flask may 404 path-segment names with slashes; direct names must
        # come back shape-complete with an error, never a 500.
        assert resp.status_code in (200, 404)
        if resp.status_code == 200:
            note = resp.get_json()["note"]
            assert note["exists"] is False


def test_notes_route_empty_profile_dir(client):
    data = client.get("/api/2x/notes").get_json()
    assert data["available"] == []
    assert data["note"]["exists"] is False


class TestShadowStalenessSignals:
    def test_fresh_collector_is_quiet(self, repo_root: Path):
        assert dashboard_db.shadow_staleness_signals(repo_root, "base", NOW) == []

    def test_stale_collector_fires_warn(self, repo_root: Path):
        import sqlite3

        conn = sqlite3.connect(repo_root / "state" / "options_shadow.db")
        conn.execute("UPDATE observations SET ts = '2026-07-01T15:07:00-04:00'")
        conn.commit()
        conn.close()
        signals = dashboard_db.shadow_staleness_signals(repo_root, "base", NOW)
        assert len(signals) == 1
        assert signals[0]["severity"] == "warn"
        assert "options_shadow" in signals[0]["message"]

    def test_absent_collectors_are_quiet(self, repo_root: Path):
        signals = dashboard_db.shadow_staleness_signals(repo_root, "2x", NOW)
        assert signals == []


class TestHaltReasonSignal:
    def test_not_halted_is_quiet(self, repo_root: Path):
        assert dashboard_db.halt_reason_signal(repo_root, "base", False) == []

    def test_halted_includes_note_excerpt(self, repo_root: Path):
        signals = dashboard_db.halt_reason_signal(repo_root, "base", True)
        assert len(signals) == 1
        assert signals[0]["severity"] == "danger"
        assert "test halt reason" in signals[0]["message"]

    def test_halted_without_reports_still_fires(self, tmp_path: Path):
        signals = dashboard_db.halt_reason_signal(tmp_path, "base", True)
        assert len(signals) == 1
        assert "HALTED" in signals[0]["message"]


class TestCriticalLogSignal:
    def test_no_log_dir_is_quiet(self, tmp_path: Path):
        assert dashboard_db.critical_log_signal(tmp_path, NOW) == []

    def test_critical_line_fires_warn(self, tmp_path: Path):
        logs = tmp_path / "logs"
        logs.mkdir()
        date = NOW.strftime("%Y%m%d")
        (logs / f"paper-{date}.log").write_text(
            "=== job=daily ===\nCRITICAL: previous run still holds the lock\n=== end rc=1 ===\n"
        )
        signals = dashboard_db.critical_log_signal(tmp_path, NOW)
        assert len(signals) == 1
        assert "holds the lock" in signals[0]["message"]

    def test_clean_log_is_quiet(self, tmp_path: Path):
        logs = tmp_path / "logs"
        logs.mkdir()
        date = NOW.strftime("%Y%m%d")
        (logs / f"paper-{date}.log").write_text("=== job=daily ===\n=== end rc=0 ===\n")
        assert dashboard_db.critical_log_signal(tmp_path, NOW) == []
