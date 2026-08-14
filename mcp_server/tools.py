"""MCP tool registrations: one function per capability, decorated with
@mcp.tool(). Every function is a thin wrapper — profile/path resolution
plus a call into dashboard.db's *_payload functions or mcp_server.debug's
new primitives — mirroring dashboard/routes.py's own "thin handler" shape,
and sharing the exact same payload functions so there is one definition of
what each response means (see dashboard/db.py's module docstring).

Unlike Flask's ProfileConverter, there is no routing-layer 404 for an
unknown profile here — each tool that takes one validates it explicitly.
"""

from __future__ import annotations

import contextlib
from pathlib import Path

from dashboard import db as dashboard_db

from . import debug as mcp_debug


def _validate_profile(profile: str) -> None:
    if profile not in dashboard_db.PROFILES:
        raise ValueError(
            f"unknown profile {profile!r}; expected one of {sorted(dashboard_db.PROFILES)}"
        )


def register_tools(mcp, repo_root: Path) -> None:
    """Registers every tool against a fixed repo_root, closed over the
    same way dashboard/app.py's create_app closes over REPO_ROOT for
    Flask — tests pass a tmp fixture path here instead of the real repo."""

    state_dir = repo_root / "state"
    reports_dir = repo_root / "reports"
    logs_dir = repo_root / "logs"

    @mcp.tool()
    def get_summary(profile: str) -> dict:
        """Composite health snapshot for a paper-trading profile ('base'
        or '2x'): mode, equity, halted flag, risk budget burndown,
        re-entry cooldowns, volatility overlay state, execution/fill
        quality, experiment stand-downs, and the raw healthcheck."""
        _validate_profile(profile)
        return dashboard_db.summary_payload(repo_root, profile)

    @mcp.tool()
    def get_equity_curve(profile: str, days: int = 90) -> dict:
        """Daily equity/cash curve (one point per calendar day, last
        snapshot of that day) plus peak-drawdown-halt and monthly-kill-
        switch reference lines. days is clamped to [1, 365]."""
        _validate_profile(profile)
        days = max(1, min(int(days), 365))
        return dashboard_db.equity_curve_payload(repo_root, profile, days)

    @mcp.tool()
    def get_orders(profile: str, since: str | None = None, limit: int = 200) -> dict:
        """Tail-polled order feed (oldest first) with fill price,
        slippage, and status. since is an ISO timestamp cursor; limit is
        clamped to [1, 500]."""
        _validate_profile(profile)
        limit = max(1, min(int(limit), 500))
        return dashboard_db.orders_payload(repo_root, profile, since, limit)

    @mcp.tool()
    def get_positions(profile: str) -> dict:
        """Current open positions: qty, price, stop level, entry date,
        unrealized P&L where derivable, stop-exempt-sleeve flag."""
        _validate_profile(profile)
        return dashboard_db.positions_payload(repo_root, profile)

    @mcp.tool()
    def get_exposure(profile: str) -> dict:
        """Latest computed gross/net exposure by sleeve, target vs
        actual, including the biggest symbol-level drift entries."""
        _validate_profile(profile)
        return dashboard_db.exposure_payload(repo_root, profile)

    @mcp.tool()
    def get_trends(profile: str, days: int = 30) -> dict:
        """Per-day execution/rejection quality trends: fill %, notional-
        weighted adverse slippage bps, average fill latency, rejection
        count, and blocked notional, bucketed by calendar day. days is
        clamped to [1, 365]."""
        _validate_profile(profile)
        days = max(1, min(int(days), 365))
        return dashboard_db.trends_payload(repo_root, profile, days)

    @mcp.tool()
    def get_round_trips(profile: str, limit: int = 100) -> dict:
        """FIFO-matched realized P&L per closed round trip (entry fill ->
        exit fill per symbol), with per-sleeve totals and win/loss counts.
        Exits whose entries predate fill recording are counted in
        "unmatched", never guessed. limit is clamped to [1, 500]."""
        _validate_profile(profile)
        limit = max(1, min(int(limit), 500))
        return dashboard_db.round_trips_payload(repo_root, profile, limit)

    @mcp.tool()
    def get_rejections(profile: str, days: int = 7, since: str | None = None) -> dict:
        """Risk-gate rejection stats: counts, blocked notional, whole-
        share-rounding/hard-to-borrow counts, top normalized reasons, and
        a sleeve/side breakdown. days is clamped to [1, 90]; since (an
        ISO timestamp) overrides days if given."""
        _validate_profile(profile)
        days = max(1, min(int(days), 90))
        return dashboard_db.rejections_payload(repo_root, profile, days, since)

    @mcp.tool()
    def get_options(profile: str, days: int = 7) -> dict:
        """Recent multi-leg options structures (with legs) and broker-vs-
        journal reconciliation alarms from state/options_*.db. days is
        clamped to [1, 90]."""
        _validate_profile(profile)
        days = max(1, min(int(days), 90))
        return dashboard_db.options_payload(repo_root, profile, days)

    @mcp.tool()
    def get_research(profile: str) -> dict:
        """Shadow-collector status (observation counts, latest observation,
        staleness vs the 21-day rule) and experiment progress toward each
        pre-registered review bar (observed vs minimum observations and
        distinct expirations)."""
        _validate_profile(profile)
        return dashboard_db.research_payload(repo_root, profile)

    @mcp.tool()
    def get_daily_notes(profile: str, name: str | None = None) -> dict:
        """List the profile's daily run notes (reports/paper*/YYYY-MM-DD.md)
        and read one (the latest by default, or the given name)."""
        _validate_profile(profile)
        return dashboard_db.notes_payload(repo_root, profile, name)

    @mcp.tool()
    def query_database(profile: str, target: str, sql: str, max_rows: int = 500) -> dict:
        """Run a single read-only SELECT/WITH query against a profile's
        journal. target is "paper" (snapshots/orders/rejections/stops/
        attribution_snapshots/leverage_recommendations tables) or
        "options" (structures/structure_legs/reconciliation_events).
        Any statement that isn't SELECT/WITH, or that tries to chain
        multiple statements, is rejected before it can touch the
        database. max_rows caps the returned rows (the "truncated" field
        in the response says whether more rows existed)."""
        _validate_profile(profile)
        paths = dashboard_db.profile_paths(repo_root, profile)
        conn = mcp_debug.open_query_target(paths, target)
        with contextlib.closing(conn):
            return mcp_debug.run_select(conn, sql, max_rows=max_rows)

    @mcp.tool()
    def read_state_file(relative_path: str) -> dict:
        """Read a JSON file under state/ (e.g. "risk_state_2x.json",
        "health_status.json", "mom_ls_targets_2x.json",
        "universe_active.json"). Use list_state_files first to see
        what's there. A missing file returns exists: false, not an
        error."""
        return mcp_debug.safe_read_json(state_dir, relative_path)

    @mcp.tool()
    def list_state_files(relative_path: str = "") -> dict:
        """List files/directories under state/ (or a subdirectory of it),
        non-recursive, to discover what's available before reading."""
        return mcp_debug.list_dir(state_dir, relative_path)

    @mcp.tool()
    def read_config(profile: str) -> dict:
        """Raw YAML text (comments included) of config.yaml/config_2x.yaml
        for the given profile — exactly what's on disk, not
        re-serialized."""
        _validate_profile(profile)
        return mcp_debug.read_config_raw(repo_root, profile)

    @mcp.tool()
    def read_report(relative_path: str) -> dict:
        """Read a file under reports/ — a backtest/research study
        (reports/*.json), an experiment registration doc
        (reports/experiments/*.json), or a daily paper-run note
        (reports/paper/*.md, reports/paper_2x/*.md). .json files are
        parsed; everything else is returned as raw text. Use
        list_reports first to see what's there."""
        if relative_path.endswith(".json"):
            return mcp_debug.safe_read_json(reports_dir, relative_path)
        return mcp_debug.safe_read_text(reports_dir, relative_path)

    @mcp.tool()
    def list_reports(relative_path: str = "") -> dict:
        """List files/directories under reports/ (or a subdirectory of
        it, e.g. "experiments", "paper", "paper_2x"), non-recursive."""
        return mcp_debug.list_dir(reports_dir, relative_path)

    @mcp.tool()
    def tail_trading_log(date: str | None = None, lines: int = 200) -> dict:
        """Tail logs/paper-<date>.log — the cron wrapper's own per-day
        log of every scripts/paper.sh job invocation (daily/daily2x/
        stops/stops2x/options_daily2x/weekly/momls2x/health/health2x),
        including which ET slot each run matched and its exit code. date
        defaults to today in UTC (YYYYMMDD format); a missing file
        returns exists: false, not an error."""
        return mcp_debug.tail_log(logs_dir, date=date, lines=lines)
