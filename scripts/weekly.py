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
from zoneinfo import ZoneInfo

from engine.attribution import execution_summary
from engine.data import REPO_ROOT
from engine.execution_timing import timing_summary

# Every ts column in these journals is written as dt.datetime.now(ET).isoformat()
# (e.g. "...T09:51:00-04:00"), never UTC. SQLite compares TEXT columns as
# strings, not as datetimes, so a cutoff computed in a different offset
# compares wrong near the boundary — a 13:51Z observation vs a naive 12:00Z
# cutoff should be included (13:51 > 12:00) but "T09:51:00-04:00" < "T12:00:00"
# as strings. Every week_ago cutoff below must be computed in ET to match.
ET = ZoneInfo("America/New_York")

DB = REPO_ROOT / "state" / "paper.db"
DB_2X = REPO_ROOT / "state" / "paper_2x.db"
OPTIONS_SHADOW_2X = REPO_ROOT / "state" / "options_shadow_2x.db"
MOMENTUM_OPTIONS_SHADOW_2X = REPO_ROOT / "state" / "momentum_options_shadow_2x.db"
EVENT_VOLATILITY_SHADOW_2X = REPO_ROOT / "state" / "event_volatility_shadow_2x.db"
ZERO_DTE_SHADOW_2X = REPO_ROOT / "state" / "zero_dte_shadow_2x.db"
REPORT_DIR = REPO_ROOT / "reports" / "paper"


def clone_allocation() -> float:
    """Sleeve weight for the 13F clone strategy; 0 when it is not trading."""
    from engine.config import load_config
    return float(load_config().sleeves_paper["sleeves"].get("clone", 0.0))


def refresh_data() -> list[str]:
    """Refresh the 13F holdings and CUSIP map, if anything still trades on them.

    Only the clone sleeve reads these, and it was retired in favour of
    equity_core. Refreshing anyway rebuilt the CUSIP map from ~36 SEC
    fail-to-deliver downloads every Sunday, left the parquet permanently
    dirty in the working tree, and could raise a CRITICAL in the weekly
    report about a sleeve that is not trading. The committed snapshot stays
    put for the backtests that use it; they can refresh explicitly.
    """
    notes = []
    if clone_allocation() <= 0:
        return ["13F/CUSIP refresh skipped: clone sleeve unallocated "
                "(backtests refresh on demand)"]
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


def short_slot_notional(top_n: int) -> float | None:
    """Dollars the mom_ls sleeve will allocate to one short, or None if unknown.

    Sized off the base profile's latest equity snapshot. The targets file is
    shared with the leveraged profiles, whose slots are strictly larger, so
    the base slot is the binding constraint — anything affordable here is
    affordable there.
    """
    if not DB.exists() or top_n <= 0:
        return None
    conn = sqlite3.connect(DB)
    try:
        row = conn.execute("SELECT equity FROM snapshots ORDER BY ts DESC LIMIT 1").fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    if not row or not row[0]:
        return None
    from engine.config import load_config
    weight = float(load_config().sleeves_paper["sleeves"]["mom_ls"])
    return float(row[0]) * weight / top_n


def _mom_ls_params(cfg) -> tuple:
    p = cfg.sleeves_paper
    return (p["mom_ls_top_n"], p["mom_ls_min_price"], p["mom_ls_min_dollar_volume"])


def build_mom_ls_targets(cfg) -> list[str]:
    """Weekly momentum ranks for the market-neutral sleeve.

    12-1 momentum over the operating-company universe, top-N long, bottom-N
    short. Short candidates must be easy-to-borrow whole-share names right
    now, verified against Alpaca per symbol — a rank list containing an
    unborrowable name would silently under-short the sleeve.

    Takes `cfg` explicitly (rather than always loading config.yaml) so a
    profile whose paper_portfolio.mom_ls_targets_file differs from the base
    profile's — e.g. the 2x lab running a different breadth per
    reports/momentum_breadth_study.json's sensitivity result — gets its own
    rebuild against its own parameters. See main()'s two call sites.
    """
    import json as _json
    import numpy as _np
    import pandas as _pd
    from engine.execute import Trader

    notes = []
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

    # Alpaca will not short fractionally, so a slot only fills if one whole
    # share fits inside it. Selection stays price-blind on purpose — the
    # capacity study found price-aware ranking no better on returns and left
    # the bottom-20 construction alone pending live data. But a name picked
    # above the slot is a short the gate will reject, so say so now rather
    # than letting the sleeve drift net long unannounced. Diagnostic only:
    # nothing here changes which names are chosen.
    max_short_px = short_slot_notional(top_n)
    if max_short_px:
        unfillable = [s for s in shorts if s in px.index and px[s] > max_short_px]
        if unfillable:
            notes.append(
                f"CRITICAL: {len(unfillable)} of {len(shorts)} shorts price above the "
                f"${max_short_px:,.2f} slot and cannot fill whole-share — sleeve will "
                f"run net long: {', '.join(unfillable[:10])}")
    else:
        notes.append("CRITICAL: no equity snapshot; short whole-share capacity unchecked")

    out = {"as_of": end.isoformat(), "long": longs, "short": shorts,
           "params": {"top_n": top_n, "min_price": min_px, "min_dollar_volume": min_dv,
                      "short_slot_notional": max_short_px}}
    path = REPO_ROOT / p["mom_ls_targets_file"]
    path.write_text(_json.dumps(out, indent=2))
    notes.append(f"mom_ls targets written: {len(longs)} long / {len(shorts)} short (easy-to-borrow)")
    return notes


def summarize_week(db_path: Path = DB) -> list[str]:
    lines = []
    if not db_path.exists():
        return ["no journal yet"]
    conn = sqlite3.connect(db_path)
    week_ago = (dt.datetime.now(ET) - dt.timedelta(days=7)).isoformat()

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
    leverage = attribution["latest_leverage_recommendation"]
    if leverage and leverage["mode"] != "off":
        vol_text = (
            f"{leverage['realized_vol']:.1%}"
            if leverage["realized_vol"] is not None else "pending"
        )
        lines.append(
            f"volatility overlay {leverage['mode']}: "
            f"{leverage['observations']} observations, realized {vol_text}, "
            f"recommended {leverage['recommended_leverage']:.2f}× "
            f"({leverage['reason']})"
        )

    # CRITICAL lines from this week's logs. Windowed by date, not by file
    # count — logs only exist for days the bot ran, so "last 8 files" silently
    # reached back further every time a run was skipped and kept resurfacing
    # long-fixed incidents as if they were current.
    log_dir = REPO_ROOT / "logs"
    cutoff = (dt.date.today() - dt.timedelta(days=7)).strftime("%Y%m%d")
    crits = []
    for f in sorted(log_dir.glob("paper-*.log")):
        if f.stem.removeprefix("paper-") < cutoff:
            continue
        crits += [f"  {f.name}: {l.strip()}" for l in f.read_text().splitlines() if "CRITICAL" in l]
    if crits:
        lines.append("CRITICAL log lines:")
        lines += crits
    return lines


def summarize_execution_timing(
    base_path: Path = DB,
    leveraged_path: Path = DB_2X,
) -> list[str]:
    if not base_path.exists() or not leveraged_path.exists():
        return ["execution timing: both journals are required"]
    with sqlite3.connect(base_path) as base, sqlite3.connect(
        leveraged_path
    ) as leveraged:
        row = timing_summary(base, leveraged)
    if not row["matched_fills"]:
        return ["execution timing: no matched MOM_LS fills since 2026-08-04"]
    base_slip = row["base_adverse_slippage_bps"]
    leveraged_slip = row["leveraged_adverse_slippage_bps"]
    gap = row["leveraged_minus_base_bps"]
    return [
        f"execution timing control: {row['matched_fills']}/"
        f"{row['control_min_pairs']} matched fills over "
        f"{row['matched_sessions']} sessions; adverse slippage base "
        f"{base_slip:+.2f} bp, 2x {leveraged_slip:+.2f} bp, gap {gap:+.2f} bp; "
        f"{row['recommendation']}"
    ]


def summarize_options_shadow(db_path: Path = OPTIONS_SHADOW_2X) -> list[str]:
    """Summarize read-only option candidates; never contacts the broker."""
    if not db_path.exists():
        return ["options shadow: no observations yet"]
    week_ago = (dt.datetime.now(ET) - dt.timedelta(days=7)).isoformat()
    with sqlite3.connect(db_path) as conn:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='candidate_observations'"
        ).fetchone()
        if not exists:
            return ["options shadow: no candidate observations yet"]
        rows = conn.execute(
            "SELECT strategy, COUNT(*), SUM(signal_enabled), SUM(qualified), "
            "AVG(executable_credit), AVG(credit_pct_of_width), "
            "AVG(maximum_loss) FROM candidate_observations "
            "WHERE ts > ? GROUP BY strategy ORDER BY strategy",
            (week_ago,),
        ).fetchall()
    if not rows:
        return ["options shadow: no observations in the last 7 days"]
    return [
        f"options shadow {strategy}: {count} observations, "
        f"{int(signals or 0)} risk-on, {int(qualified or 0)} qualified; "
        f"average executable credit ${credit or 0:.2f} "
        f"({credit_pct or 0:.1%} of width), max loss ${max_loss or 0:,.2f}"
        for strategy, count, signals, qualified, credit, credit_pct, max_loss in rows
    ]


def summarize_momentum_options_shadow(
    db_path: Path = MOMENTUM_OPTIONS_SHADOW_2X,
) -> list[str]:
    if not db_path.exists():
        return ["momentum-options shadow: no observations yet"]
    week_ago = (dt.datetime.now(ET) - dt.timedelta(days=7)).isoformat()
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT rank_side, COUNT(*), SUM(qualified), AVG(maximum_loss), "
            "AVG(reward_to_risk) FROM observations WHERE ts > ? "
            "GROUP BY rank_side ORDER BY rank_side",
            (week_ago,),
        ).fetchall()
    if not rows:
        return ["momentum-options shadow: no observations in the last 7 days"]
    return [
        f"momentum-options {side}: {count} observations, "
        f"{int(qualified or 0)} qualified; average max loss "
        f"${max_loss or 0:,.2f}, reward/risk {reward_risk or 0:.2f}"
        for side, count, qualified, max_loss, reward_risk in rows
    ]


def summarize_event_volatility_shadow(
    db_path: Path = EVENT_VOLATILITY_SHADOW_2X,
    *,
    stale_after_days: int = 21,
) -> list[str]:
    """All-time per-event breakdown, not a 7-day window: events are sparse
    and calendar-scheduled (a handful a quarter), so a rolling week is often
    legitimately empty even when the collector is healthy — a week-window
    would false-alarm on normal gaps. Staleness is instead judged against
    the most recent observation regardless of event date, so a collector
    that has actually stopped running (vs. one correctly waiting for the
    next scheduled event) is still distinguishable."""
    if not db_path.exists():
        return ["event-volatility shadow: no observations yet"]
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT event_name, event_date, COUNT(*), "
            "AVG(implied_break_even_move_pct) FROM observations "
            "GROUP BY event_name, event_date ORDER BY event_date"
        ).fetchall()
        latest = conn.execute("SELECT MAX(ts) FROM observations").fetchone()[0]
    if not rows:
        return ["event-volatility shadow: no observations yet"]
    lines = [
        f"event-volatility {name} {date}: {count} observations, "
        f"average implied break-even move {move:.2%}"
        for name, date, count, move in rows
    ]
    if latest:
        age_days = (dt.datetime.now(ET) - dt.datetime.fromisoformat(latest)).days
        if age_days > stale_after_days:
            lines.append(
                f"STALE: last observation {age_days}d ago (as of {latest}) — "
                f"expected within {stale_after_days}d even accounting for gaps "
                "between scheduled events"
            )
    return lines


def summarize_zero_dte_shadow(db_path: Path = ZERO_DTE_SHADOW_2X) -> list[str]:
    """7-day window, matching summarize_options_shadow/
    summarize_momentum_options_shadow: this collector fires roughly daily
    (unlike event-volatility's sparse calendar), so an all-time aggregate
    let a fully-dead collector read as a healthy, growing-looking count —
    a "0 observations in the last 7 days" line is only reachable now if it
    actually stopped producing rows."""
    if not db_path.exists():
        return ["0DTE shadow: no observations yet"]
    week_ago = (dt.datetime.now(ET) - dt.timedelta(days=7)).isoformat()
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT COUNT(*), SUM(structure_qualified), AVG(atm_straddle_ask), "
            "AVG(executable_credit), AVG(maximum_loss) FROM observations "
            "WHERE ts > ?",
            (week_ago,),
        ).fetchone()
    if not row or not row[0]:
        return ["0DTE shadow: no observations in the last 7 days"]
    return [
        f"0DTE surface: {row[0]} observations, {int(row[1] or 0)} condors "
        f"quote-qualified; average straddle ask ${row[2] or 0:.2f}, "
        f"condor credit ${row[3] or 0:.2f}, max loss ${row[4] or 0:,.2f}"
    ]


def main() -> None:
    import argparse

    from engine.config import load_config

    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--mom-ls-only", action="store_true",
        help="Skip data refresh and the weekly report; rebuild only the 2x "
             "lab's mid-week MOM_LS targets. Backs the twice-weekly rebuild "
             "cadence trial in reports/mom_ls_cadence_study.json — screening-"
             "tier evidence, not a hard-gate promotion (config.yaml/base "
             "stays weekly-only, unchanged).",
    )
    args = ap.parse_args()

    if args.mom_ls_only:
        cfg_2x = load_config(REPO_ROOT / "config_2x.yaml")
        for note in build_mom_ls_targets(cfg_2x):
            print(note)
        return

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    today = dt.date.today()
    body = ["# Weekly paper report " + today.isoformat(), "", "## Data refresh"]
    body += [f"- {n}" for n in refresh_data()]

    base_cfg = load_config()
    try:
        body += [f"- {n}" for n in build_mom_ls_targets(base_cfg)]
    except Exception as exc:
        body += [f"- CRITICAL: mom_ls target build failed (base): {type(exc).__name__}: {exc}"]

    # The 2x lab only gets its own rebuild when its mom_ls_targets_file
    # actually points somewhere else — running it against the same file the
    # base profile just wrote would either duplicate work for an identical
    # result (both profiles unchanged) or silently clobber the shared file
    # with whichever profile's parameters ran last (profiles diverged but
    # nobody gave the lab its own output path — a misconfiguration, not a
    # thing to paper over).
    cfg_2x = load_config(REPO_ROOT / "config_2x.yaml")
    base_file = base_cfg.sleeves_paper.get("mom_ls_targets_file")
    file_2x = cfg_2x.sleeves_paper.get("mom_ls_targets_file")
    if file_2x != base_file:
        try:
            body += [f"- {n}" for n in build_mom_ls_targets(cfg_2x)]
        except Exception as exc:
            body += [f"- CRITICAL: mom_ls target build failed (2x): {type(exc).__name__}: {exc}"]
    elif _mom_ls_params(cfg_2x) != _mom_ls_params(base_cfg):
        body.append(
            "- CRITICAL: config_2x.yaml's mom_ls construction "
            "(top_n/min_price/min_dollar_volume) differs from config.yaml's "
            "but both share the same mom_ls_targets_file — 2x rebuild "
            "skipped to avoid clobbering; give config_2x.yaml its own "
            "mom_ls_targets_file"
        )

    body += ["", "## Base account"]
    body += [f"- {l}" for l in summarize_week(DB)]
    body += ["", "## 2× account"]
    body += [f"- {l}" for l in summarize_week(DB_2X)]
    body += ["", "## Execution timing experiment"]
    body += [f"- {l}" for l in summarize_execution_timing()]
    body += ["", "## Options experiments (read-only shadow)"]
    body += [f"- {l}" for l in summarize_options_shadow()]
    body += [f"- {l}" for l in summarize_momentum_options_shadow()]
    body += [f"- {l}" for l in summarize_event_volatility_shadow()]
    body += [f"- {l}" for l in summarize_zero_dte_shadow()]
    out = REPORT_DIR / f"weekly-{today}.md"
    out.write_text("\n".join(body) + "\n")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
