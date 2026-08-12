from __future__ import annotations

import sqlite3
from pathlib import Path

from dashboard.app import create_app

# Same shape as conftest.SCHEMA's orders/rejections, but WITHOUT the newer
# migration columns — mirrors an un-migrated live deployment. Inlined here
# (rather than imported from conftest) since tests/ has no __init__.py and
# relative imports between test modules aren't reliable under pytest's
# default rootdir import mode.
UNMIGRATED_SCHEMA = """
CREATE TABLE snapshots(
    ts TEXT, equity REAL, cash REAL, positions TEXT, diag TEXT);
CREATE TABLE orders(
    ts TEXT, symbol TEXT, side TEXT, sleeve TEXT, qty REAL, notional REAL,
    limit_price REAL, stop_price REAL, reason TEXT, alpaca_id TEXT, status TEXT);
CREATE TABLE rejections(
    ts TEXT, symbol TEXT, reason TEXT);
CREATE TABLE stops(
    symbol TEXT PRIMARY KEY, stop_price REAL, entry_price REAL, entry_date TEXT, sleeve TEXT);
"""


def test_summary_reports_equity_mode_and_budget(client):
    resp = client.get("/api/base/summary")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["mode"] == "paper"
    assert data["equity"] == 10500.0
    assert data["halted"] is False
    assert data["risk_budget"]["daily_loss_limit_pct"] == 4.0
    # day_start_equity 10400 -> equity 10500 is UP, not a drawdown, so 0 used.
    assert data["risk_budget"]["daily_used_pct"] == 0.0
    assert data["health"]["healthy"] is True


def test_summary_reentry_cooldown_reflects_config_block_days(client):
    resp = client.get("/api/base/summary")
    cooldown = resp.get_json()["reentry_cooldown"]
    assert len(cooldown) == 1
    assert cooldown[0]["symbol"] == "XYZ"
    assert cooldown[0]["days_remaining"] > 0


def test_equity_curve_has_one_point_per_day_and_reference_lines(client):
    resp = client.get("/api/base/equity-curve?days=90")
    data = resp.get_json()
    assert len(data["points"]) == 3
    assert data["points"][-1]["equity"] == 10500.0
    assert data["reference_lines"]["peak_drawdown_halt"] is not None
    assert data["reference_lines"]["peak_drawdown_halt"] < 10500.0


def test_orders_tail_polling_only_returns_newer_rows(client):
    first = client.get("/api/base/orders").get_json()
    assert len(first["orders"]) == 2
    cursor = first["latest_ts"]
    assert cursor is not None

    second = client.get(f"/api/base/orders?since={cursor}").get_json()
    assert second["orders"] == []
    assert second["latest_ts"] == cursor


def test_positions_flags_stop_exempt_sleeve_instead_of_fabricating_pl(client):
    resp = client.get("/api/base/positions")
    positions = {p["symbol"]: p for p in resp.get_json()["positions"]}

    spy = positions["SPY"]
    assert spy["cost_basis_available"] is True
    assert spy["stop_exempt_sleeve"] is False
    assert spy["unrealized_pl"] is not None

    aaoi = positions["AAOI"]
    assert aaoi["sleeve"] == "mom_ls"
    assert aaoi["stop_exempt_sleeve"] is True
    assert aaoi["cost_basis_available"] is False
    assert aaoi["unrealized_pl"] is None


def test_exposure_returns_latest_attribution_snapshot(client):
    resp = client.get("/api/base/exposure")
    data = resp.get_json()["latest_exposure"]
    assert data is not None
    assert "mom_ls" in data["by_sleeve"]
    assert data["by_sleeve"]["mom_ls"]["unrealized_pl"] == 20.0


def test_rejections_groups_numerically_templated_reasons(client):
    resp = client.get("/api/base/rejections?days=7")
    data = resp.get_json()
    reasons = {r["reason"]: r["count"] for r in data["top_reasons"]}
    # "price 3.21 below minimum 5.00" and "price 4.10 below minimum 5.00"
    # must collapse into one templated row with count 2.
    assert reasons.get("price # below minimum #") == 2
    assert reasons.get("short notional # rounds below # whole share") == 1


def test_unconfigured_profile_returns_empty_data_not_an_error(client):
    """The 2x fixture profile has a config but no journal yet."""
    resp = client.get("/api/2x/summary")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["equity"] is None
    assert data["risk_budget"] is None

    orders_resp = client.get("/api/2x/orders")
    assert orders_resp.get_json()["orders"] == []

    positions_resp = client.get("/api/2x/positions")
    assert positions_resp.get_json()["positions"] == []


def test_unknown_profile_404s_at_the_routing_layer(client):
    resp = client.get("/api/notaprofile/summary")
    assert resp.status_code == 404


def test_routes_tolerate_an_unmigrated_database_without_500ing(tmp_path: Path):
    """Mirrors engine/attribution.py's own PRAGMA-driven tolerance: a
    deployment that hasn't run current code yet lacks several newer
    columns/tables. Routes must degrade gracefully, not crash."""
    import shutil

    from engine.config import REPO_ROOT as REAL_REPO_ROOT

    shutil.copy(REAL_REPO_ROOT / "config.yaml", tmp_path / "config.yaml")
    shutil.copy(REAL_REPO_ROOT / "config_2x.yaml", tmp_path / "config_2x.yaml")
    state = tmp_path / "state"
    state.mkdir()

    conn = sqlite3.connect(state / "paper.db")
    conn.executescript(UNMIGRATED_SCHEMA)
    conn.execute(
        "INSERT INTO snapshots VALUES (?,?,?,?,?)",
        ("2026-08-03T09:47:00-04:00", 10000.0, 5000.0,
         '{"SPY": {"qty": 1, "px": 730.0}}', "{}"),
    )
    conn.execute(
        "INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("2026-08-03T09:47:01-04:00", "SPY", "buy", "equity_core", 1, 730.0,
         731.0, 0.0, "clean", "id-1", "filled"),
    )
    conn.execute(
        "INSERT INTO rejections VALUES (?,?,?)",
        ("2026-08-03T09:47:00-04:00", "FOO", "price 3.21 below minimum 5.00"),
    )
    conn.commit()
    conn.close()

    app = create_app(repo_root=tmp_path)
    app.config["TESTING"] = True
    with app.test_client() as c:
        for path in (
            "/api/base/summary", "/api/base/equity-curve", "/api/base/orders",
            "/api/base/positions", "/api/base/exposure", "/api/base/rejections",
        ):
            resp = c.get(path)
            assert resp.status_code == 200, f"{path} returned {resp.status_code}"
