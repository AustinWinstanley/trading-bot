"""Read-only forward-trade recorder for the pre-registered IWM
compression-breakout SPRT.

reports/iwm_compression_breakout_forward_test_registration.json committed
the judging methodology (Wald SPRT, fixed alpha/beta/mu0/mu1/sigma) but
explicitly not the build: "IWM compression_breakout must first exist as a
live signal generator before any trades can be observed." This module is
that generator — observation only. It records the trades the frozen
specification WOULD have made, priced at the registration's 5bp/leg stress
cost; it holds no position and submits nothing. No method in this module
submits, replaces, cancels, exercises, or otherwise mutates a broker
account (enforced statically by tests/test_shadow_read_only.py).

The signal and simulator are imported from the accepted implementation
(backtest.intraday_strategy_study.compression_breakout_signal +
backtest.intraday.simulate_fixed_horizon) rather than reimplemented, so
the forward trades are computed by the exact code path the historical
study used — there is no variant here to validate.

Because the specification is deterministic on completed five-minute bars
(signal on a completed bar, enter next bar's open, exit on a bar close in
the same session), post-close reconstruction from the day's bars produces
exactly the trades live intrabar observation would. This recorder
therefore only processes sessions strictly BEFORE the current ET date:
every eligible session's bars are final, the job is insensitive to what
hour it runs, and it needs no market-clock slot guard or EDT/EST crontab
pair. The one-day latency is irrelevant to a test whose registered horizon
is measured in years.

Frozen-window discipline: sessions before 2026-08-13 are never recorded,
however the journal or bar cache came to exist — feeding pre-registration
data to the SPRT is exactly the violation AGENTS.md documents. A boundary
crossing is reported loudly but changes nothing automatically: promotion
to shadow is a human-reviewed step per the registration.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
from zoneinfo import ZoneInfo

import pandas as pd

from backtest.intraday import (
    STRESS_COST_BPS_PER_LEG,
    prepare_bars,
    simulate_fixed_horizon,
)
from backtest.intraday_strategy_study import compression_breakout_signal
from backtest.iwm_compression_breakout_forward_test import monitor
from engine.data import AlpacaClient, REPO_ROOT

ET = ZoneInfo("America/New_York")

SYMBOL = "IWM"
# Frozen by the registration: the study's accepted hold horizon for
# compression_breakout (backtest/intraday_strategy_study.py STRATEGIES).
HOLD_BARS = 18
# The registration judges returns net of the 5bp/leg STRESS cost, not the
# 2bp primary convention.
COST_BPS_PER_LEG = STRESS_COST_BPS_PER_LEG
# First legitimate forward session (AGENTS.md's re-frozen window).
FORWARD_START = dt.date(2026, 8, 13)

DB = REPO_ROOT / "state/iwm_breakout_forward.db"


def db(path=DB) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript("""
    BEGIN;
    -- Every completed session examined, signal or not: distinguishes "no
    -- breakout fired that day" from "the job never ran", and stops old
    -- sessions from being refetched forever.
    CREATE TABLE IF NOT EXISTS sessions_processed(
        session TEXT PRIMARY KEY,
        bars INTEGER NOT NULL,
        traded INTEGER NOT NULL,
        recorded_ts TEXT NOT NULL
    );
    -- At most one trade per session by construction (the study's own
    -- one-trade-per-session rule in simulate_fixed_horizon).
    CREATE TABLE IF NOT EXISTS forward_trades(
        session TEXT PRIMARY KEY REFERENCES sessions_processed(session),
        signal_ts TEXT NOT NULL,
        entry_ts TEXT NOT NULL,
        exit_ts TEXT NOT NULL,
        direction INTEGER NOT NULL,
        entry_price REAL NOT NULL,
        exit_price REAL NOT NULL,
        gross_return REAL NOT NULL,
        net_return REAL NOT NULL,
        recorded_ts TEXT NOT NULL
    );
    """)
    return conn


def eligible_sessions(
    available: list[str], processed: set[str], *, today_et: dt.date
) -> list[str]:
    """Sessions to record now: present in the fetched bars, not yet
    journaled, on or after the frozen forward start, and strictly before
    today (ET) so their bars are final regardless of run time."""
    out = []
    for session in sorted(available):
        session_date = dt.date.fromisoformat(session)
        if session_date < FORWARD_START or session_date >= today_et:
            continue
        if session in processed:
            continue
        out.append(session)
    return out


def record(conn: sqlite3.Connection, bars, sessions: list[str], *, now: dt.datetime) -> list[dict]:
    """Journal the frozen spec's trades for each session, idempotently.

    `bars` is a prepare_bars() frame that may span many sessions; the
    signal and simulator run over the whole frame exactly as the study ran
    them, and only rows for `sessions` are written.
    """
    signal = compression_breakout_signal(bars)
    trades = simulate_fixed_horizon(
        bars, signal, hold_bars=HOLD_BARS, cost_bps_per_leg=COST_BPS_PER_LEG
    )
    by_session = {row["session"]: row for _, row in trades.iterrows()}
    recorded = []
    for session in sessions:
        day = bars[bars["session"] == session]
        row = by_session.get(session)
        conn.execute(
            "INSERT OR IGNORE INTO sessions_processed VALUES (?,?,?,?)",
            (session, len(day), 1 if row is not None else 0, now.isoformat()),
        )
        if row is not None:
            conn.execute(
                "INSERT OR IGNORE INTO forward_trades VALUES (?,?,?,?,?,?,?,?,?,?)",
                (session, row["signal_ts"], row["entry_ts"], row["exit_ts"],
                 int(row["direction"]), float(row["entry_price"]),
                 float(row["exit_price"]), float(row["gross_return"]),
                 float(row["net_return"]), now.isoformat()),
            )
            recorded.append(dict(row))
    conn.commit()
    return recorded


def forward_returns(conn: sqlite3.Connection) -> list[float]:
    """Every journaled forward trade's net return, in execution order —
    the exact input shape backtest.iwm_compression_breakout_forward_test
    .monitor() registered."""
    return [
        float(r[0]) for r in conn.execute(
            "SELECT net_return FROM forward_trades ORDER BY session"
        ).fetchall()
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="compute and print, write nothing")
    args = parser.parse_args()

    now = dt.datetime.now(ET)
    today_et = now.date()
    # Read the processed set without creating or touching the journal — a
    # dry run must stay mutation-free, including schema creation. A missing
    # file or a schema-less one both mean "nothing processed yet".
    processed: set[str] = set()
    if DB.exists():
        ro = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        try:
            processed = {
                str(r[0]) for r in ro.execute(
                    "SELECT session FROM sessions_processed"
                ).fetchall()
            }
        except sqlite3.OperationalError:
            pass
        finally:
            ro.close()

    # Fetch a window from the earliest possibly-unprocessed session; the
    # journal's PRIMARY KEYs make overlap harmless.
    last = max(processed) if processed else None
    fetch_start = (
        dt.date.fromisoformat(last) + dt.timedelta(days=1) if last else FORWARD_START
    )
    if fetch_start >= today_et:
        print(f"iwm_breakout_forward: no completed sessions after {last} yet")
        if not args.dry_run:
            conn = db()
            print(json.dumps(monitor(forward_returns(conn))))
        return

    frames = AlpacaClient().get_bars(
        [SYMBOL], fetch_start, today_et, timeframe="5Min", adjustment="all"
    )
    bars = prepare_bars(frames.get(SYMBOL, pd.DataFrame()))
    available = sorted(set(bars["session"])) if not bars.empty else []
    sessions = eligible_sessions(available, processed, today_et=today_et)

    if args.dry_run:
        print(f"DRY RUN: would process sessions {sessions}")
        return

    conn = db()
    recorded = record(conn, bars, sessions, now=now)
    for trade in recorded:
        print(
            f"forward trade {trade['session']}: dir={trade['direction']} "
            f"net={trade['net_return']:+.5f}"
        )
    print(
        f"iwm_breakout_forward: processed {len(sessions)} session(s), "
        f"{len(recorded)} trade(s)"
    )

    result = monitor(forward_returns(conn))
    print(json.dumps(result))
    if result["decision"] != "continue_monitoring":
        # Loud, but no automatic action: the registration's stopping rule
        # promotes to a human-reviewed shadow step, never directly to
        # config. This line is scraped by the weekly-report CRITICAL sweep.
        print(f"CRITICAL: IWM breakout SPRT boundary crossed: {result['decision']} "
              "— human review required (see registration JSON)")


if __name__ == "__main__":
    main()
