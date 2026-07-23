"""Sunday job: refresh slow-moving data and write the weekly summary.

  * re-download any new 13F data sets and rebuild holdings (quarterly filings
    arrive on their own schedule; checking weekly is cheap because everything
    already cached is skipped)
  * refresh the CUSIP map
  * summarise the week from state/paper.db, including every gate rejection and
    any CRITICAL lines from the logs — the lessons-learned raw material
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from pathlib import Path

from engine.data import REPO_ROOT

DB = REPO_ROOT / "state" / "paper.db"
REPORT_DIR = REPO_ROOT / "reports" / "paper"


def refresh_data() -> list[str]:
    notes = []
    try:
        from engine.thirteenf import build_holdings, cusip_ticker_map
        h = build_holdings(refresh=True)
        notes.append(f"13F holdings refreshed: {len(h):,} rows, "
                     f"latest filing {h['filing_date'].max().date()}")
        m = cusip_ticker_map(refresh=True)
        notes.append(f"CUSIP map refreshed: {len(m):,} entries")
    except Exception as exc:
        notes.append(f"CRITICAL: 13F refresh failed: {type(exc).__name__}: {exc}")
    return notes


def summarize_week() -> list[str]:
    lines = []
    if not DB.exists():
        return ["no journal yet"]
    conn = sqlite3.connect(DB)
    week_ago = (dt.datetime.now() - dt.timedelta(days=7)).isoformat()

    snaps = conn.execute(
        "SELECT ts, equity, cash FROM snapshots WHERE ts > ? ORDER BY ts", (week_ago,)
    ).fetchall()
    if snaps:
        first_eq, last_eq = snaps[0][1], snaps[-1][1]
        lines.append(f"equity {first_eq:,.2f} -> {last_eq:,.2f} "
                     f"({(last_eq / first_eq - 1) if first_eq else 0:+.2%}) over {len(snaps)} snapshots")

    orders = conn.execute(
        "SELECT side, COUNT(*), SUM(notional) FROM orders WHERE ts > ? GROUP BY side", (week_ago,)
    ).fetchall()
    for side, n, tot in orders:
        lines.append(f"orders {side}: {n} totalling ${tot or 0:,.2f}")

    rejs = conn.execute(
        "SELECT reason, COUNT(*) FROM rejections WHERE ts > ? "
        "GROUP BY reason ORDER BY COUNT(*) DESC LIMIT 10", (week_ago,)
    ).fetchall()
    if rejs:
        lines.append("top gate rejections:")
        lines += [f"  {n:>4}x {reason[:100]}" for reason, n in rejs]

    # CRITICAL lines from this week's logs
    log_dir = REPO_ROOT / "logs"
    crits = []
    for f in sorted(log_dir.glob("paper-*.log"))[-8:]:
        crits += [f"  {f.name}: {l.strip()}" for l in f.read_text().splitlines() if "CRITICAL" in l]
    if crits:
        lines.append("CRITICAL log lines:")
        lines += crits
    return lines


def main() -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    today = dt.date.today()
    body = ["# Weekly paper report " + today.isoformat(), "", "## Data refresh"]
    body += [f"- {n}" for n in refresh_data()]
    body += ["", "## Week in review"]
    body += [f"- {l}" for l in summarize_week()]
    out = REPORT_DIR / f"weekly-{today}.md"
    out.write_text("\n".join(body) + "\n")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
