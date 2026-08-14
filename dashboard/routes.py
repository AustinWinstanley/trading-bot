"""All dashboard routes: one page, one JSON API blueprint.

Every /api/<profile>/... handler is a thin wrapper: parse request.args,
call the matching dashboard.db.*_payload function, jsonify() the result.
The composition logic (which config/state/journal fields make up each
response) lives entirely in dashboard/db.py, shared with mcp_server/'s
MCP tools — see that module's docstring for why.
"""

from __future__ import annotations

from flask import Blueprint, current_app, jsonify, render_template, request

from . import db as dashboard_db

bp = Blueprint("dashboard", __name__)


def _repo_root():
    return current_app.config["REPO_ROOT"]


@bp.get("/")
def index():
    return render_template("index.html")


@bp.get("/api/<profile:profile>/summary")
def summary(profile: str):
    return jsonify(dashboard_db.summary_payload(_repo_root(), profile))


@bp.get("/api/<profile:profile>/equity-curve")
def equity_curve(profile: str):
    days = max(1, min(int(request.args.get("days", 90)), 365))
    return jsonify(dashboard_db.equity_curve_payload(_repo_root(), profile, days))


@bp.get("/api/<profile:profile>/orders")
def orders(profile: str):
    since = request.args.get("since")
    limit = max(1, min(int(request.args.get("limit", 200)), 500))
    return jsonify(dashboard_db.orders_payload(_repo_root(), profile, since, limit))


@bp.get("/api/<profile:profile>/positions")
def positions(profile: str):
    return jsonify(dashboard_db.positions_payload(_repo_root(), profile))


@bp.get("/api/<profile:profile>/exposure")
def exposure(profile: str):
    return jsonify(dashboard_db.exposure_payload(_repo_root(), profile))


@bp.get("/api/<profile:profile>/trends")
def trends(profile: str):
    days = max(1, min(int(request.args.get("days", 30)), 365))
    return jsonify(dashboard_db.trends_payload(_repo_root(), profile, days))


@bp.get("/api/<profile:profile>/round-trips")
def round_trips(profile: str):
    limit = max(1, min(int(request.args.get("limit", 100)), 500))
    return jsonify(dashboard_db.round_trips_payload(_repo_root(), profile, limit))


@bp.get("/api/<profile:profile>/rejections")
def rejections(profile: str):
    days = max(1, min(int(request.args.get("days", 7)), 90))
    since = request.args.get("since")
    return jsonify(dashboard_db.rejections_payload(_repo_root(), profile, days, since))


@bp.get("/api/<profile:profile>/options")
def options(profile: str):
    days = max(1, min(int(request.args.get("days", 7)), 90))
    return jsonify(dashboard_db.options_payload(_repo_root(), profile, days))


@bp.get("/api/<profile:profile>/research")
def research(profile: str):
    return jsonify(dashboard_db.research_payload(_repo_root(), profile))


@bp.get("/api/<profile:profile>/notes")
def notes(profile: str):
    return jsonify(dashboard_db.notes_payload(_repo_root(), profile))


@bp.get("/api/<profile:profile>/notes/<name>")
def note(profile: str, name: str):
    # name is allowlist-validated inside notes_payload before any path
    # logic — an invalid name returns a shape-complete error, not a 500.
    return jsonify(dashboard_db.notes_payload(_repo_root(), profile, name))
