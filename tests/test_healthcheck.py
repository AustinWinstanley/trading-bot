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


def test_option_position_is_not_flagged_for_missing_stop():
    """Option contracts are defined-risk by their spread structure's own
    maximum_loss, never by an equity-style stop order — scripts/
    options_daily.py has no stop-submission path for legs at all. Flagging
    them here was pure noise from day one of live options trading."""
    problems = assess_health(
        account_status="ACTIVE",
        positions=[{"symbol": "SPY260918P00751000", "asset_class": "us_option"}],
        open_orders=[],
        fallback_stops=set(),
        last_snapshot=NOW,
        now=NOW,
        max_age_hours=72,
    )
    assert problems == []


def test_option_exemption_does_not_excuse_an_equity_position():
    problems = assess_health(
        account_status="ACTIVE",
        positions=[
            {"symbol": "SPY260918P00751000", "asset_class": "us_option"},
            {"symbol": "QQQ", "asset_class": "us_equity"},
        ],
        open_orders=[],
        fallback_stops=set(),
        last_snapshot=NOW,
        now=NOW,
        max_age_hours=72,
    )
    assert any("QQQ" in p for p in problems)
    assert not any("SPY260918P00751000" in p for p in problems)


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


def test_open_option_structures_handles_missing_and_schemaless_journal(tmp_path):
    from scripts.healthcheck import open_option_structures

    # No file at all: options trading never ran — healthy, nothing to reconcile.
    missing = tmp_path / "options_2x.db"
    assert open_option_structures(missing) == []
    assert not missing.exists()  # the check must not create the file

    # A 0-byte schema-less file (a bare sqlite3.connect creates one as a side
    # effect of connecting) once crashed the whole 2x healthcheck with
    # "no such table: structures" before health_status was written.
    schemaless = tmp_path / "schemaless.db"
    schemaless.touch()
    assert open_option_structures(schemaless) == []
    assert schemaless.stat().st_size == 0  # opened read-only, never written


def test_open_option_structures_reads_a_real_journal(tmp_path, monkeypatch):
    import scripts.options_daily as options_daily
    from scripts.healthcheck import open_option_structures

    db_path = tmp_path / "options_2x.db"
    monkeypatch.setattr(options_daily, "DB", db_path)
    conn = options_daily.db()  # creates the real schema
    conn.execute(
        "INSERT INTO structures(structure_id, experiment, strategy, underlying, "
        "expiration_date, contracts, requested_contracts, credit, maximum_loss, "
        "opened_ts, status) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("s1", options_daily.EXPERIMENT_NAME, options_daily.STRATEGY_KEY, "SPY",
         "2026-09-18", 1, 1, 0.85, 4.15, "2026-08-14T10:00:00", "open"),
    )
    conn.execute(
        "INSERT INTO structure_legs(structure_id, symbol, side, position_intent, "
        "ratio_qty) VALUES (?,?,?,?,?)",
        ("s1", "SPY260918P00560000", "sell", "sell_to_open", 1),
    )
    conn.commit()
    conn.close()

    structures = open_option_structures(db_path)
    assert [s["structure_id"] for s in structures] == ["s1"]
    assert structures[0]["legs"][0]["position_intent"] == "sell_to_open"
