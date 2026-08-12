"""Rebuild a deleted daily Markdown report from the SQLite journal.

This is deliberately journal-only: it does not load credentials, contact the
broker, compute targets, or submit orders. Some transient run fields (broker
open-order count, reconciliation count, and submission exceptions) were never
stored in SQLite and are labelled as unavailable rather than invented.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
from pathlib import Path
from zoneinfo import ZoneInfo

from engine.data import REPO_ROOT

ET = ZoneInfo("America/New_York")
PROFILE_PATHS = {
    "base": (REPO_ROOT / "state/paper.db", REPO_ROOT / "reports/paper"),
    "2x": (REPO_ROOT / "state/paper_2x.db", REPO_ROOT / "reports/paper_2x"),
}


def _json(value, fallback):
    try:
        return json.loads(value) if value else fallback
    except (TypeError, json.JSONDecodeError):
        return fallback


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def render_report(conn: sqlite3.Connection, day: dt.date) -> str:
    conn.row_factory = sqlite3.Row
    prefix = day.isoformat()
    snapshots = conn.execute(
        "SELECT * FROM snapshots WHERE substr(ts, 1, 10)=? ORDER BY ts", (prefix,)
    ).fetchall()
    if not snapshots:
        raise ValueError(f"journal has no snapshots for {prefix}")

    blocks = [f"# Paper {prefix}", ""]
    has_orders = _has_table(conn, "orders")
    has_rejections = _has_table(conn, "rejections")
    has_attribution = _has_table(conn, "attribution_snapshots")
    has_leverage = _has_table(conn, "leverage_recommendations")

    for snapshot in snapshots:
        ts = snapshot["ts"]
        diag = _json(snapshot["diag"], {})
        positions = _json(snapshot["positions"], {})
        stops_only = bool(diag.get("stops_only"))
        submitted = (
            conn.execute("SELECT COUNT(*) FROM orders WHERE ts=?", (ts,)).fetchone()[0]
            if has_orders else 0
        )
        rejected = (
            conn.execute("SELECT COUNT(*) FROM rejections WHERE ts=?", (ts,)).fetchone()[0]
            if has_rejections else 0
        )

        lines = [
            f"## run {ts}" + (" (stops-only)" if stops_only else ""),
            f"- equity ${snapshot['equity']:,.2f} | cash ${snapshot['cash']:,.2f} | positions {len(positions)}",
        ]
        if not stops_only:
            lines.append(
                f"- sleeves {diag.get('sleeve_counts', {})} | "
                f"invested {float(diag.get('total_weight', 0)):.0%} | "
                f"cash target {float(diag.get('cash_weight', 1)):.0%}"
            )
        lines += [
            f"- journal reconstruction: submitted {submitted} | rejected {rejected} | recorded proposals {submitted + rejected}",
            "- transient submission-failure, broker-open-order, and reconciliation counts are unavailable from the journal",
        ]

        if not stops_only and has_attribution:
            exposure = conn.execute(
                "SELECT * FROM attribution_snapshots WHERE ts=? ORDER BY rowid DESC LIMIT 1",
                (ts,),
            ).fetchone()
            if exposure:
                lines += [
                    f"- target exposure long {exposure['target_long']:.1%} | short {exposure['target_short']:.1%} | gross {exposure['target_gross']:.1%}",
                    f"- actual exposure long {exposure['actual_long']:.1%} | short {exposure['actual_short']:.1%} | gross {exposure['actual_gross']:.1%}",
                ]
        if not stops_only and has_leverage:
            leverage = conn.execute(
                "SELECT * FROM leverage_recommendations WHERE ts=? ORDER BY rowid DESC LIMIT 1",
                (ts,),
            ).fetchone()
            if leverage:
                realized = (
                    f"{leverage['realized_vol']:.1%}"
                    if leverage["realized_vol"] is not None else "pending"
                )
                lines.append(
                    f"- volatility overlay {leverage['mode']} | observations "
                    f"{leverage['observations']} | realized {realized} | "
                    f"recommended {leverage['recommended_leverage']:.2f}×"
                )
        blocks.extend(lines + [""])

    blocks += [
        "_Regenerated from the SQLite journal; transient broker/run fields that were not persisted are explicitly marked unavailable._",
        "",
    ]
    return "\n".join(blocks)


def regenerate(profile: str, day: dt.date, *, force: bool = False) -> Path:
    db_path, report_dir = PROFILE_PATHS[profile]
    if not db_path.exists():
        raise FileNotFoundError(f"journal not found: {db_path}")
    out = report_dir / f"{day.isoformat()}.md"
    if out.exists() and not force:
        raise FileExistsError(f"report already exists: {out}; pass --force to replace it")
    with sqlite3.connect(db_path) as conn:
        body = render_report(conn, day)
    out.parent.mkdir(parents=True, exist_ok=True)
    temporary = out.with_suffix(".md.tmp")
    temporary.write_text(body)
    temporary.replace(out)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=PROFILE_PATHS, default="base")
    parser.add_argument(
        "--date",
        type=dt.date.fromisoformat,
        default=dt.datetime.now(ET).date(),
        help="ET trading date (default: today)",
    )
    parser.add_argument("--force", action="store_true", help="replace an existing report")
    args = parser.parse_args()
    try:
        out = regenerate(args.profile, args.date, force=args.force)
    except (FileNotFoundError, FileExistsError, ValueError) as exc:
        parser.error(str(exc))
    print(f"regenerated {out}")


if __name__ == "__main__":
    main()
