import sqlite3
import subprocess

import scripts.weekly as weekly

PAPER_SH = weekly.REPO_ROOT / "scripts" / "paper.sh"


def _snapshot_db(path, equity):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE snapshots(ts TEXT, equity REAL, cash REAL, pos TEXT, diag TEXT)")
    conn.execute("INSERT INTO snapshots VALUES (?,?,?,?,?)", ("2026-08-03T09:47", equity, 0, "{}", "{}"))
    conn.commit()
    conn.close()


def test_short_slot_sized_off_latest_equity(tmp_path, monkeypatch):
    db = tmp_path / "paper.db"
    _snapshot_db(db, 10_000.0)
    monkeypatch.setattr(weekly, "DB", db)
    # 10,000 equity * 0.15 mom_ls weight / 20 slots
    assert weekly.short_slot_notional(20) == 75.0


def test_short_slot_is_none_without_a_snapshot(tmp_path, monkeypatch):
    db = tmp_path / "paper.db"
    _snapshot_db(db, 10_000.0)
    conn = sqlite3.connect(db)
    conn.execute("DELETE FROM snapshots")
    conn.commit()
    conn.close()
    monkeypatch.setattr(weekly, "DB", db)
    # None must not silently become an unbounded price cap.
    assert weekly.short_slot_notional(20) is None


def test_short_slot_handles_missing_db(tmp_path, monkeypatch):
    monkeypatch.setattr(weekly, "DB", tmp_path / "absent.db")
    assert weekly.short_slot_notional(20) is None


def _run_slot(slot, tmp_path):
    """Run paper.sh with an unknown job so nothing trades; rc 2 means it got past the guard."""
    proc = subprocess.run(
        [str(PAPER_SH), "__notajob__", slot],
        capture_output=True,
        env={"PATH": "/usr/bin:/bin", "PAPER_LOG_DIR": str(tmp_path)},
    )
    lock = weekly.REPO_ROOT / "state" / "paper-__notajob__.lock"
    lock.unlink(missing_ok=True)
    return proc.returncode


def test_slot_guard_skips_the_off_dst_firing(tmp_path):
    # 03:03 ET is never within 5 minutes of a real run, so the guard must
    # no-op (rc 0) before reaching the unknown-job branch (rc 2).
    assert _run_slot("03:03", tmp_path) == 0


def test_slot_guard_runs_when_et_matches(tmp_path):
    import datetime as dt
    from zoneinfo import ZoneInfo

    now_et = dt.datetime.now(ZoneInfo("America/New_York")).strftime("%H:%M")
    assert _run_slot(now_et, tmp_path) == 2


def _fake_thirteenf(called):
    """Stand-in for engine.thirteenf that records calls instead of hitting SEC."""
    import types

    mod = types.ModuleType("engine.thirteenf")

    def build_holdings(refresh=False):
        called.append(("holdings", refresh))
        raise RuntimeError("network disabled in tests")

    def cusip_ticker_map(refresh=False):
        called.append(("cusip", refresh))
        raise RuntimeError("network disabled in tests")

    mod.build_holdings = build_holdings
    mod.cusip_ticker_map = cusip_ticker_map
    return mod


def test_refresh_skipped_when_clone_sleeve_is_unallocated(monkeypatch):
    """The live config retires the clone sleeve, so nothing should hit SEC."""
    import sys

    called = []
    monkeypatch.setattr(weekly, "clone_allocation", lambda: 0.0)
    monkeypatch.setitem(sys.modules, "engine.thirteenf", _fake_thirteenf(called))

    notes = weekly.refresh_data()

    assert called == []                            # nothing downloaded
    assert notes == ["13F/CUSIP refresh skipped: clone sleeve unallocated "
                     "(backtests refresh on demand)"]


def test_refresh_runs_when_clone_sleeve_is_allocated(monkeypatch):
    import sys

    called = []
    monkeypatch.setattr(weekly, "clone_allocation", lambda: 0.15)
    monkeypatch.setitem(sys.modules, "engine.thirteenf", _fake_thirteenf(called))

    notes = weekly.refresh_data()

    assert called == [("holdings", True)]          # it did try, and refreshed
    assert notes[0].startswith("CRITICAL: 13F refresh failed")


def test_live_config_has_no_clone_allocation():
    """Guards the assumption the skip depends on."""
    assert weekly.clone_allocation() == 0.0
