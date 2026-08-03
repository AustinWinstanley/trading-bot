import datetime as dt
from zoneinfo import ZoneInfo

from scripts.healthcheck import assess_health

ET = ZoneInfo("America/New_York")
NOW = dt.datetime(2026, 7, 23, 12, 0, tzinfo=ET)


def test_healthy_position_with_broker_stop():
    problems = assess_health(
        account_status="ACTIVE",
        positions=[{"symbol": "SPY"}],
        open_orders=[{"symbol": "SPY", "type": "stop"}],
        fallback_stops=set(),
        last_snapshot=NOW - dt.timedelta(hours=2),
        now=NOW,
        max_age_hours=72,
    )
    assert problems == []


def test_healthcheck_reports_stale_snapshot_and_unprotected_position():
    problems = assess_health(
        account_status="ACTIVE",
        positions=[{"symbol": "SPY"}],
        open_orders=[],
        fallback_stops=set(),
        last_snapshot=NOW - dt.timedelta(hours=80),
        now=NOW,
        max_age_hours=72,
    )
    assert any("snapshot" in problem for problem in problems)
    assert any("no broker or fallback stop" in problem for problem in problems)


def test_fallback_stop_is_accepted():
    problems = assess_health(
        account_status="ACTIVE",
        positions=[{"symbol": "SPY"}],
        open_orders=[],
        fallback_stops={"SPY"},
        last_snapshot=NOW,
        now=NOW,
        max_age_hours=72,
    )
    assert problems == []


def test_stale_parent_order_is_critical():
    problems = assess_health(
        account_status="ACTIVE",
        positions=[],
        open_orders=[{
            "symbol": "SPY", "type": "limit", "id": "abc",
            "submitted_at": (NOW - dt.timedelta(hours=25)).isoformat(),
        }],
        fallback_stops=set(),
        last_snapshot=NOW,
        now=NOW,
        max_age_hours=72,
    )
    assert any("non-protective order" in problem for problem in problems)


def test_pristine_profile_can_bootstrap_upgrade():
    problems = assess_health(
        account_status="ACTIVE",
        positions=[],
        open_orders=[],
        fallback_stops=set(),
        last_snapshot=None,
        now=NOW,
        max_age_hours=72,
        allow_pristine=True,
        journal_is_pristine=True,
    )
    assert problems == []


def test_allow_pristine_does_not_hide_a_used_account_without_snapshot():
    problems = assess_health(
        account_status="ACTIVE",
        positions=[{"symbol": "SPY"}],
        open_orders=[],
        fallback_stops=set(),
        last_snapshot=None,
        now=NOW,
        max_age_hours=72,
        allow_pristine=True,
        journal_is_pristine=True,
    )
    assert "no journal snapshot exists" in problems


def test_unstopped_sleeve_position_is_not_flagged():
    """mom_ls positions carry no stop by design; alerting on them is noise."""
    problems = assess_health(
        account_status="ACTIVE",
        positions=[{"symbol": "MU"}],
        open_orders=[],
        fallback_stops=set(),
        last_snapshot=NOW,
        now=NOW,
        max_age_hours=72,
        unstopped_symbols={"MU"},
    )
    assert problems == []


def test_unstopped_set_does_not_excuse_other_positions():
    problems = assess_health(
        account_status="ACTIVE",
        positions=[{"symbol": "MU"}, {"symbol": "SPY"}],
        open_orders=[],
        fallback_stops=set(),
        last_snapshot=NOW,
        now=NOW,
        max_age_hours=72,
        unstopped_symbols={"MU"},
    )
    assert any("SPY" in p for p in problems)
    assert not any("MU" in p for p in problems)


def test_unstopped_from_journal_uses_the_latest_entry():
    import sqlite3

    from scripts.healthcheck import unstopped_from_journal

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE orders(ts TEXT, symbol TEXT, side TEXT, sleeve TEXT)")
    conn.executemany(
        "INSERT INTO orders VALUES (?,?,?,?)",
        [
            ("2026-07-01", "MU", "buy", "tsmom"),    # older, stopped sleeve
            ("2026-08-01", "MU", "buy", "mom_ls"),   # latest wins
            ("2026-08-01", "SPY", "buy", "equity_core"),
        ],
    )
    assert unstopped_from_journal(conn, frozenset({"mom_ls"})) == {"MU"}
    assert unstopped_from_journal(conn, frozenset()) == set()
