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

from engine.attribution import execution_summary
from engine.data import REPO_ROOT

DB = REPO_ROOT / "state" / "paper.db"
DB_2X = REPO_ROOT / "state" / "paper_2x.db"
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


def build_mom_ls_targets() -> list[str]:
    """Weekly momentum ranks for the market-neutral sleeve.

    12-1 momentum over the operating-company universe, top-N long, bottom-N
    short. Short candidates must be easy-to-borrow whole-share names right
    now, verified against Alpaca per symbol — a rank list containing an
    unborrowable name would silently under-short the sleeve.
    """
    import json as _json
    import numpy as _np
    import pandas as _pd
    from engine.config import load_config
    from engine.execute import Trader

    notes = []
    cfg = load_config()
    p = cfg.sleeves_paper
    top_n = int(p["mom_ls_top_n"])
    min_px, min_dv = float(p["mom_ls_min_price"]), float(p["mom_ls_min_dollar_volume"])

    cls = _json.loads((REPO_ROOT / "state" / "universe_classified.json").read_text())
    stocks = cls["stocks"]
    t = Trader()
    end = dt.date.today()
    start = end - dt.timedelta(days=420)
    bars = t.get_bars(stocks, start, end)
    close = _pd.DataFrame({s2: d["close"] for s2, d in bars.items() if len(d) >= 260})
    volume = _pd.DataFrame({s2: d["volume"] for s2, d in bars.items() if len(d) >= 260})
    notes.append(f"mom_ls universe priced: {close.shape[1]:,} names with >=260 bars")

    mom = (close.shift(21) / close.shift(252) - 1).iloc[-1]
    px = close.iloc[-1]
    dv = (close * volume).rolling(20).mean().iloc[-1]
    ok = (px >= min_px) & (dv >= min_dv) & mom.notna()
    ranked = mom[ok].sort_values(ascending=False)
    notes.append(f"eligible after filters: {len(ranked):,}")

    longs = list(ranked.head(top_n).index)
    shorts = []
    for sym in ranked.index[::-1]:                 # worst momentum first
        if len(shorts) >= top_n:
            break
        try:
            a = t.get_asset(sym)
            if a.get("shortable") and a.get("easy_to_borrow"):
                shorts.append(sym)
        except Exception:
            continue

    out = {"as_of": end.isoformat(), "long": longs, "short": shorts,
           "params": {"top_n": top_n, "min_price": min_px, "min_dollar_volume": min_dv}}
    path = REPO_ROOT / p["mom_ls_targets_file"]
    path.write_text(_json.dumps(out, indent=2))
    notes.append(f"mom_ls targets written: {len(longs)} long / {len(shorts)} short (easy-to-borrow)")
    return notes


def summarize_week(db_path: Path = DB) -> list[str]:
    lines = []
    if not db_path.exists():
        return ["no journal yet"]
    conn = sqlite3.connect(db_path)
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

    attribution = execution_summary(conn, week_ago)
    execution = attribution["overall"]
    if execution["orders"]:
        slippage = execution["adverse_slippage_bps"]
        slip_text = f"{slippage:+.2f} bp" if slippage is not None else "pending"
        lines.append(
            f"execution: {execution['filled_orders']}/{execution['orders']} orders "
            f"filled; {execution['fill_pct']:.1f}% of approved notional; "
            f"approval {execution['approval_pct']:.1f}% of requested; "
            f"adverse slippage {slip_text}"
        )
        for sleeve, row in attribution["by_sleeve"].items():
            sleeve_slip = row["adverse_slippage_bps"]
            sleeve_slip_text = (
                f"{sleeve_slip:+.2f} bp"
                if sleeve_slip is not None else "pending"
            )
            lines.append(
                f"  {sleeve}: {row['filled_orders']}/{row['orders']} filled, "
                f"{row['fill_pct']:.1f}% notional, slippage {sleeve_slip_text}"
            )
    rejects = attribution["rejections"]
    if rejects["whole_share_rounding"] or rejects["hard_to_borrow"]:
        lines.append(
            f"short execution gaps: {rejects['whole_share_rounding']} whole-share "
            f"rounding, {rejects['hard_to_borrow']} hard-to-borrow; "
            f"${rejects['requested_notional']:,.2f} total rejected notional"
        )
    exposure = attribution["latest_exposure"]
    if exposure:
        lines.append(
            f"latest exposure target L/S/G "
            f"{exposure['target_long']:.1%}/{exposure['target_short']:.1%}/"
            f"{exposure['target_gross']:.1%}; actual "
            f"{exposure['actual_long']:.1%}/{exposure['actual_short']:.1%}/"
            f"{exposure['actual_gross']:.1%}"
        )
        for sleeve, row in exposure["by_sleeve"].items():
            lines.append(
                f"  {sleeve}: actual gross {row['gross']:.1%}, "
                f"net {row['net']:+.1%}, unrealized P&L "
                f"${row['unrealized_pl']:+,.2f}"
            )

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
    try:
        body += [f"- {n}" for n in build_mom_ls_targets()]
    except Exception as exc:
        body += [f"- CRITICAL: mom_ls target build failed: {type(exc).__name__}: {exc}"]
    body += ["", "## Base account"]
    body += [f"- {l}" for l in summarize_week(DB)]
    body += ["", "## 2× account"]
    body += [f"- {l}" for l in summarize_week(DB_2X)]
    out = REPORT_DIR / f"weekly-{today}.md"
    out.write_text("\n".join(body) + "\n")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
