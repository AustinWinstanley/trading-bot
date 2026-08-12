import datetime as dt
import sqlite3

from scripts.regenerate_daily_report import render_report


def test_render_report_rebuilds_all_runs_and_labels_unrecoverable_fields():
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
    CREATE TABLE snapshots(ts, equity, cash, positions, diag);
    CREATE TABLE orders(ts, symbol);
    CREATE TABLE rejections(ts, symbol);
    CREATE TABLE attribution_snapshots(
        ts, equity, target_long, target_short, target_gross,
        actual_long, actual_short, actual_gross,
        target_by_sleeve, actual_by_sleeve, targets, actual_weights,
        largest_symbol_gaps);
    CREATE TABLE leverage_recommendations(
        ts, profile, mode, observations, target_vol, realized_vol,
        recommended_scale, recommended_leverage, ready, reason);
    INSERT INTO snapshots VALUES(
        '2026-08-12T09:47:00-04:00', 10100, 1000, '{"SPY": {}}',
        '{"sleeve_counts": {"equity_core": 1}, "total_weight": 0.8, "cash_weight": 0.2}');
    INSERT INTO snapshots VALUES(
        '2026-08-12T13:35:00-04:00', 10150, 900, '{}',
        '{"sleeve_counts": {}, "total_weight": 0, "cash_weight": 1}');
    INSERT INTO orders VALUES('2026-08-12T09:47:00-04:00', 'SPY');
    INSERT INTO rejections VALUES('2026-08-12T09:47:00-04:00', 'FXE');
    INSERT INTO attribution_snapshots VALUES(
        '2026-08-12T09:47:00-04:00', 10100, 1, .15, 1.15,
        .94, .06, 1, '{}', '{}', '{}', '{}', '[]');
    INSERT INTO leverage_recommendations VALUES(
        '2026-08-12T09:47:00-04:00', 'base', 'off', 14, .12, NULL, 1, 1, 0, 'pending');
    """)

    report = render_report(conn, dt.date(2026, 8, 12))
    assert report.startswith("# Paper 2026-08-12")
    assert report.count("## run ") == 2
    assert "submitted 1 | rejected 1 | recorded proposals 2" in report
    assert "target exposure long 100.0% | short 15.0% | gross 115.0%" in report
    assert "transient submission-failure" in report


def test_render_report_refuses_date_without_snapshots():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE snapshots(ts, equity, cash, positions, diag)")
    try:
        render_report(conn, dt.date(2026, 8, 12))
    except ValueError as exc:
        assert "no snapshots" in str(exc)
    else:
        raise AssertionError("missing date should fail")
