"""Forward recorder for the registered IWM compression-breakout SPRT.

The trade math itself is not re-tested here — the recorder imports the
accepted compression_breakout_signal + simulate_fixed_horizon rather than
reimplementing them (tests/test_intraday*.py own that behavior). These
tests cover what the recorder adds: session eligibility (frozen-window
start, completed-sessions-only), idempotent journaling, the no-signal
session record, and the SPRT input ordering.
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from backtest.intraday import prepare_bars
from scripts.iwm_breakout_forward import (
    FORWARD_START,
    db,
    eligible_sessions,
    forward_returns,
    record,
)

ET = ZoneInfo("America/New_York")
NOW = dt.datetime(2026, 8, 14, 18, 0, tzinfo=ET)


def _session_bars(day: dt.date, *, breakout: bool) -> pd.DataFrame:
    """One synthetic 78-bar regular session.

    Bars 0-5 sit in a tight range (well under the 0.4%-of-open compression
    bound). With breakout=True, bar 7's close pops above the opening-range
    high, so the frozen spec fires long at bar 7 and enters at bar 8's open.
    """
    idx = pd.date_range(
        start=dt.datetime(day.year, day.month, day.day, 9, 30, tzinfo=ET),
        periods=78, freq="5min",
    )
    open_ = np.full(78, 100.0)
    high = np.full(78, 100.1)
    low = np.full(78, 99.9)
    close = np.full(78, 100.0)
    if breakout:
        close[7] = 100.5
        open_[8] = 100.5
        close[8:] = 100.8
        high[7:] = 101.0
    volume = np.full(78, 10_000.0)
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        index=idx,
    )


def _bars(days: dict[dt.date, bool]) -> pd.DataFrame:
    frames = [_session_bars(day, breakout=fired) for day, fired in days.items()]
    return prepare_bars(pd.concat(frames))


def test_eligible_sessions_respects_frozen_start_and_completed_only():
    available = ["2026-08-12", "2026-08-13", "2026-08-14"]
    got = eligible_sessions(available, set(), today_et=dt.date(2026, 8, 14))
    # 08-12 predates the frozen forward window; 08-14 (today) is not final.
    assert got == ["2026-08-13"]
    # Already-processed sessions never repeat.
    assert eligible_sessions(available, {"2026-08-13"}, today_et=dt.date(2026, 8, 14)) == []


def test_record_journals_breakout_and_quiet_sessions_idempotently(tmp_path):
    conn = db(tmp_path / "fwd.db")
    bars = _bars({dt.date(2026, 8, 13): True, dt.date(2026, 8, 14): False})
    sessions = ["2026-08-13", "2026-08-14"]

    recorded = record(conn, bars, sessions, now=NOW)
    assert [t["session"] for t in recorded] == ["2026-08-13"]
    assert recorded[0]["direction"] == 1
    # Net return carries the registered 5bp/leg stress cost: entry 100.5 ->
    # session-end close 100.8 gross, minus 2 legs * 5bp.
    expected_net = (100.8 / 100.5 - 1) - 2 * 5.0 / 10_000
    assert abs(recorded[0]["net_return"] - expected_net) < 1e-12

    # Both sessions journaled as processed; only one carries a trade.
    processed = dict(conn.execute(
        "SELECT session, traded FROM sessions_processed"
    ).fetchall())
    assert processed == {"2026-08-13": 1, "2026-08-14": 0}

    # Re-recording the same sessions changes nothing (idempotent).
    record(conn, bars, sessions, now=NOW)
    assert conn.execute("SELECT COUNT(*) FROM forward_trades").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM sessions_processed").fetchone()[0] == 2


def test_forward_returns_are_in_session_order(tmp_path):
    conn = db(tmp_path / "fwd.db")
    # Insert out of order; the SPRT registration requires execution order.
    for session, net in (("2026-08-20", 0.002), ("2026-08-13", -0.001)):
        conn.execute(
            "INSERT INTO sessions_processed VALUES (?,?,?,?)",
            (session, 78, 1, NOW.isoformat()),
        )
        conn.execute(
            "INSERT INTO forward_trades VALUES (?,?,?,?,?,?,?,?,?,?)",
            (session, "t", "t", "t", 1, 100.0, 100.0, net, net, NOW.isoformat()),
        )
    assert forward_returns(conn) == [-0.001, 0.002]


def test_forward_start_matches_the_registered_frozen_window():
    # AGENTS.md: 2026-08-13 onward is the frozen final-validation window.
    assert FORWARD_START == dt.date(2026, 8, 13)
