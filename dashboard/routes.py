"""All dashboard routes: one page, one JSON API blueprint.

Every /api/<profile>/... handler opens its own read-only connection inside
the request (see dashboard.db.open_ro) and closes it before returning —
no connection or config object is ever cached across requests, so a
`base` request and a `2x` request can never race on shared state.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import sqlite3

from flask import Blueprint, current_app, jsonify, render_template, request

from engine.attribution import execution_summary
from engine.config import load_config

from . import db as dashboard_db

bp = Blueprint("dashboard", __name__)


def _paths(profile: str) -> dashboard_db.ProfilePaths:
    repo_root = current_app.config["REPO_ROOT"]
    return dashboard_db.profile_paths(repo_root, profile)


def _open_or_none(paths: dashboard_db.ProfilePaths) -> sqlite3.Connection | None:
    try:
        return dashboard_db.open_ro(paths.db_path)
    except sqlite3.OperationalError:
        # No journal yet for this profile — a valid, expected state right
        # after a fresh checkout, not an error.
        return None


@bp.get("/")
def index():
    return render_template("index.html")


@bp.get("/api/<profile:profile>/summary")
def summary(profile: str):
    paths = _paths(profile)
    # validate_experiments=False: this container never mounts reports/
    # (docker-compose.yml intentionally keeps the image minimal), and a
    # monitoring page must never 500 because a trading-safety business
    # rule (registration doc must exist) is unmet — that's a real state
    # to report, not a reason to go dark. See engine/config.py's
    # _parse_experiments docstring.
    cfg = load_config(paths.config_path, validate_experiments=False)
    risk_state = dashboard_db.load_json(paths.risk_state_path, default={})
    health = dashboard_db.load_json(paths.health_status_path, default=None)

    equity = None
    last_run_ts = None
    latest_leverage = None
    execution = None
    conn = _open_or_none(paths)
    if conn is not None:
        with contextlib.closing(conn):
            snap = dashboard_db.latest_snapshot(conn)
            if snap:
                equity = snap["equity"]
                last_run_ts = snap["ts"]
            es = execution_summary(conn, dashboard_db.EPOCH)
            latest_leverage = es["latest_leverage_recommendation"]
            # Computed by the call above since day one and previously
            # discarded — the whole fill-quality story (fill %, approval %,
            # adverse slippage bps, overall and per sleeve) for free.
            execution = {"overall": es["overall"], "by_sleeve": es["by_sleeve"]}

    budget = dashboard_db.risk_budget(
        cfg.risk.daily_loss_limit_pct,
        cfg.risk.monthly_kill_switch_pct,
        cfg.risk.peak_drawdown_halt_pct,
        risk_state,
        equity,
    )
    cooldown = dashboard_db.reentry_cooldown(
        risk_state, cfg.risk.loss_reentry_block_days, dt.date.today()
    )
    overlay_cfg = cfg.sleeves_paper.get("volatility_overlay", {}) if cfg.sleeves_paper else {}

    return jsonify({
        "profile": profile,
        "mode": cfg.mode,
        "gross_leverage": cfg.sleeves_paper.get("gross_leverage") if cfg.sleeves_paper else None,
        "halted": bool(risk_state.get("halted", False)),
        "equity": equity,
        "last_run_ts": last_run_ts,
        "risk_budget": budget,
        "reentry_cooldown": cooldown,
        "volatility_overlay": {
            "configured_mode": overlay_cfg.get("mode", "off"),
            "min_observations": overlay_cfg.get("min_observations"),
            "latest_recommendation": latest_leverage,
        },
        "execution": execution,
        "experiments": dashboard_db.experiments_status(risk_state),
        "health": health,
    })


@bp.get("/api/<profile:profile>/equity-curve")
def equity_curve(profile: str):
    paths = _paths(profile)
    # validate_experiments=False: this container never mounts reports/
    # (docker-compose.yml intentionally keeps the image minimal), and a
    # monitoring page must never 500 because a trading-safety business
    # rule (registration doc must exist) is unmet — that's a real state
    # to report, not a reason to go dark. See engine/config.py's
    # _parse_experiments docstring.
    cfg = load_config(paths.config_path, validate_experiments=False)
    risk_state = dashboard_db.load_json(paths.risk_state_path, default={})
    days = max(1, min(int(request.args.get("days", 90)), 365))

    points: list[dict] = []
    conn = _open_or_none(paths)
    if conn is not None:
        with contextlib.closing(conn):
            points = dashboard_db.equity_curve(conn, days=days)

    peak = risk_state.get("peak_equity")
    month_start = risk_state.get("month_start_equity")
    return jsonify({
        "points": points,
        "reference_lines": {
            "peak_drawdown_halt": (
                round(peak * (1 - cfg.risk.peak_drawdown_halt_pct), 2) if peak else None
            ),
            "monthly_kill_switch": (
                round(month_start * (1 - cfg.risk.monthly_kill_switch_pct), 2)
                if month_start else None
            ),
        },
    })


@bp.get("/api/<profile:profile>/orders")
def orders(profile: str):
    paths = _paths(profile)
    since = request.args.get("since")
    limit = max(1, min(int(request.args.get("limit", 200)), 500))

    conn = _open_or_none(paths)
    if conn is None:
        return jsonify({"orders": [], "latest_ts": since})
    with contextlib.closing(conn):
        result = dashboard_db.recent_orders(conn, since=since, limit=limit)
    return jsonify(result)


@bp.get("/api/<profile:profile>/positions")
def positions(profile: str):
    paths = _paths(profile)
    # validate_experiments=False: this container never mounts reports/
    # (docker-compose.yml intentionally keeps the image minimal), and a
    # monitoring page must never 500 because a trading-safety business
    # rule (registration doc must exist) is unmet — that's a real state
    # to report, not a reason to go dark. See engine/config.py's
    # _parse_experiments docstring.
    cfg = load_config(paths.config_path, validate_experiments=False)
    conn = _open_or_none(paths)
    if conn is None:
        return jsonify({"positions": []})
    with contextlib.closing(conn):
        pos = dashboard_db.current_positions(conn, cfg.risk.stop_exempt_sleeves)
    return jsonify({"positions": pos})


@bp.get("/api/<profile:profile>/exposure")
def exposure(profile: str):
    paths = _paths(profile)
    conn = _open_or_none(paths)
    if conn is None:
        return jsonify({"latest_exposure": None})
    with contextlib.closing(conn):
        es = execution_summary(conn, dashboard_db.EPOCH)
    return jsonify({"latest_exposure": es["latest_exposure"]})


@bp.get("/api/<profile:profile>/rejections")
def rejections(profile: str):
    paths = _paths(profile)
    days = max(1, min(int(request.args.get("days", 7)), 90))
    since = request.args.get("since") or (
        dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
    ).isoformat()

    conn = _open_or_none(paths)
    if conn is None:
        return jsonify({"count": 0, "requested_notional": 0.0,
                         "whole_share_rounding": 0, "hard_to_borrow": 0,
                         "top_reasons": [], "by_sleeve_side": []})
    with contextlib.closing(conn):
        es = execution_summary(conn, since)
        top_reasons = dashboard_db.top_rejection_reasons(conn, since)
        by_sleeve_side = dashboard_db.rejections_by_sleeve_side(conn, since)
    return jsonify({
        **es["rejections"],
        "top_reasons": top_reasons,
        "by_sleeve_side": by_sleeve_side,
    })


@bp.get("/api/<profile:profile>/options")
def options(profile: str):
    """Options structures + reconciliation alarms from state/options_*.db.

    The file may be missing (base profile, or a lab that has never run
    scripts.options_daily), or exist as a 0-byte file with no tables (it
    was touched but the first run hasn't created the schema) — both are
    the same valid "nothing yet" state, never an error.
    """
    paths = _paths(profile)
    days = max(1, min(int(request.args.get("days", 7)), 90))
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).isoformat()

    empty = {"structures": [], "reconciliation_events": []}
    try:
        conn = dashboard_db.open_ro(paths.options_db_path)
    except sqlite3.OperationalError:
        return jsonify(empty)
    with contextlib.closing(conn):
        structures = dashboard_db.options_structures(conn)
        events = dashboard_db.reconciliation_events(conn, since)
    return jsonify({"structures": structures, "reconciliation_events": events})
