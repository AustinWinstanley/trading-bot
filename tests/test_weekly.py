import datetime as dt
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
    # macOS does not ship util-linux flock. A successful stub lets this test
    # isolate the ET slot guard and unknown-job dispatch without depending on
    # the developer machine's package set; production still uses real flock.
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    fake_flock = fake_bin / "flock"
    fake_flock.write_text("#!/bin/sh\nexit 0\n")
    fake_flock.chmod(0o755)
    proc = subprocess.run(
        [str(PAPER_SH), "__notajob__", slot],
        capture_output=True,
        env={
            "PATH": f"{fake_bin}:/usr/bin:/bin",
            "PAPER_LOG_DIR": str(tmp_path),
            "PAPER_BOT_ROOT": str(weekly.REPO_ROOT),
        },
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


def test_execution_timing_summary_surfaces_control_progress(tmp_path):
    base = tmp_path / "base.db"
    leveraged = tmp_path / "leveraged.db"
    for path, ts, fill in (
        (base, "2026-08-10T09:47:00-04:00", 100.01),
        (leveraged, "2026-08-10T09:51:00-04:00", 100.08),
    ):
        conn = sqlite3.connect(path)
        conn.execute("""
            CREATE TABLE orders(
                ts, symbol, side, sleeve, filled_qty, reference_price,
                filled_avg_price)
        """)
        conn.execute(
            "INSERT INTO orders VALUES (?,?,?,?,?,?,?)",
            (ts, "A", "buy", "mom_ls", 1, 100, fill),
        )
        conn.commit()
        conn.close()
    lines = weekly.summarize_execution_timing(base, leveraged)
    assert "1/100 matched fills" in lines[0]
    assert "gap +7.00 bp" in lines[0]


def test_options_shadow_summary_reports_qualification_rate(tmp_path):
    db = tmp_path / "options.db"
    conn = sqlite3.connect(db)
    conn.execute("""
        CREATE TABLE candidate_observations(
            ts, strategy, profile, spot, account_equity,
            options_buying_power, signal_enabled, expiration_date,
            short_symbol, short_strike, short_delta, long_symbol, long_strike,
            executable_credit, maximum_loss, credit_pct_of_width,
            within_risk_budget, credit_qualified, qualified, raw,
            PRIMARY KEY(ts, strategy))
    """)
    now = __import__("datetime").datetime.now(
        __import__("datetime").timezone.utc
    ).isoformat()
    conn.execute(
        "INSERT INTO candidate_observations VALUES (" + ",".join("?" * 20) + ")",
        (now, "delta", "2x", 700, 10_000, 3_000, 1, "2026-09-18",
         "SHORT", 630, -.2, "LONG", 625, .8, 420, .16, 1, 1, 1, "{}"),
    )
    conn.commit()
    conn.close()

    lines = weekly.summarize_options_shadow(db)
    assert len(lines) == 1
    assert "1 observations, 1 risk-on, 1 qualified" in lines[0]
    assert "$0.80 (16.0% of width)" in lines[0]


def test_momentum_options_summary_reports_both_rank_sides(tmp_path):
    db = tmp_path / "momentum-options.db"
    conn = sqlite3.connect(db)
    conn.execute("""
        CREATE TABLE observations(
            ts, profile, rank_side, rank, underlying, direction,
            expiration_date, long_symbol, long_strike, long_delta,
            short_symbol, short_strike, short_delta, net_debit,
            maximum_profit, maximum_loss, reward_to_risk, qualified,
            reason, raw, PRIMARY KEY(ts, rank_side))
    """)
    now = __import__("datetime").datetime.now(
        __import__("datetime").timezone.utc
    ).isoformat()
    conn.execute(
        "INSERT INTO observations VALUES (" + ",".join("?" * 20) + ")",
        (now, "2x", "long", 1, "A", "bull_call", "2026-10-16",
         "L", 10, .6, "S", 15, .35, 1, 400, 100, 4, 1, "qualified", "{}"),
    )
    conn.commit()
    conn.close()
    lines = weekly.summarize_momentum_options_shadow(db)
    assert lines == [
        "momentum-options long: 1 observations, 1 qualified; "
        "average max loss $100.00, reward/risk 4.00"
    ]


def _zero_dte_db(path, ts):
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE observations(
            ts TEXT PRIMARY KEY, profile TEXT, spot REAL,
            atm_straddle_ask REAL, executable_credit REAL,
            credit_pct_of_width REAL, maximum_loss REAL,
            structure_qualified INTEGER, directional_enabled INTEGER, raw TEXT)
    """)
    conn.execute(
        "INSERT INTO observations VALUES (?,?,?,?,?,?,?,?,?,?)",
        (ts, "2x", 700.0, 5.0, 1.2, 0.24, 380.0, 1, 0, "{}"),
    )
    conn.commit()
    conn.close()


def test_zero_dte_summary_reports_recent_observations(tmp_path):
    db = tmp_path / "zero-dte.db"
    _zero_dte_db(db, dt.datetime.now(weekly.ET).isoformat())
    lines = weekly.summarize_zero_dte_shadow(db)
    assert len(lines) == 1
    assert "1 observations, 1 condors" in lines[0]


def test_zero_dte_summary_flags_a_dead_collector_instead_of_reading_healthy(tmp_path):
    # A real but stale row (well outside the 7-day window) must NOT be
    # aggregated as if it were current — that's exactly how a dead
    # collector read as healthy before this fix (all-time aggregate, no
    # recency check at all).
    db = tmp_path / "zero-dte.db"
    stale = dt.datetime.now(weekly.ET) - dt.timedelta(days=30)
    _zero_dte_db(db, stale.isoformat())
    assert weekly.summarize_zero_dte_shadow(db) == [
        "0DTE shadow: no observations in the last 7 days"
    ]


def _event_volatility_db(path, ts):
    conn = sqlite3.connect(path)
    conn.execute("""
        CREATE TABLE observations(
            ts TEXT, profile TEXT, event_name TEXT, event_date TEXT,
            days_to_event INTEGER, spot REAL, expiration_date TEXT,
            strike REAL, call_symbol TEXT, put_symbol TEXT,
            straddle_debit REAL, implied_break_even_move_pct REAL, raw TEXT,
            PRIMARY KEY(ts, event_name, event_date))
    """)
    conn.execute(
        "INSERT INTO observations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (ts, "2x", "CPI", "2026-09-10", 5, 700.0, "2026-09-11",
         700.0, "CALL", "PUT", 8.0, 0.0114, "{}"),
    )
    conn.commit()
    conn.close()


def test_event_volatility_summary_is_quiet_when_recently_active(tmp_path):
    # A gap since the scheduled event is normal, not stale — only checked
    # against the freshness threshold, not a rolling week.
    db = tmp_path / "event-vol.db"
    recent = dt.datetime.now(weekly.ET) - dt.timedelta(days=10)
    _event_volatility_db(db, recent.isoformat())
    lines = weekly.summarize_event_volatility_shadow(db, stale_after_days=21)
    assert len(lines) == 1
    assert "STALE" not in lines[0]


def test_event_volatility_summary_flags_a_dead_collector(tmp_path):
    db = tmp_path / "event-vol.db"
    stale = dt.datetime.now(weekly.ET) - dt.timedelta(days=45)
    _event_volatility_db(db, stale.isoformat())
    lines = weekly.summarize_event_volatility_shadow(db, stale_after_days=21)
    assert any(line.startswith("STALE") for line in lines)


def test_mom_ls_params_reflects_config_divergence():
    from engine.config import load_config
    from engine.data import REPO_ROOT

    base = load_config()
    two_x = load_config(REPO_ROOT / "config_2x.yaml")
    # base is the unchanged control (breadth 20); 2x is running the lab's
    # experiment-tier breadth-15 candidate from momentum_breadth_study.json
    # — see config_2x.yaml's mom_ls_top_n comment.
    assert weekly._mom_ls_params(base)[0] == 20
    assert weekly._mom_ls_params(two_x)[0] == 15
    assert weekly._mom_ls_params(base) != weekly._mom_ls_params(two_x)
    # A distinct targets file is required whenever the params diverge —
    # main() relies on this to decide whether the 2x rebuild is safe to run
    # at all (see the CRITICAL clobbering guard in main()).
    assert (
        base.sleeves_paper["mom_ls_targets_file"]
        != two_x.sleeves_paper["mom_ls_targets_file"]
    )
