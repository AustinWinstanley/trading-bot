"""Per-sleeve mark-to-market P&L decomposition, and the automated portion
of the absolute-return paper-validation kill rules.

Promotes the ad hoc script this repo's 2026-09-14/15 strategy overhaul used
to diagnose the MOM_LS stand-down (docs/research.md, "Live results and the
2026-09-14 stand-down") into a permanent tool `scripts/weekly.py` calls
every week, so the kill rules in
`reports/absolute_return_paper_validation_registration.json` are actually
checkable without a manual query each time — Phase 4 of that overhaul's
plan.

Identity, per symbol per snapshot interval [t-1, t], signed quantities
(shorts negative):

    contribution = qty_{t-1} * (mark_t - mark_{t-1})
                 + sum_fills dq_i * (mark_t - fill_price_i)

Proof: MV_t - MV_{t-1} - cashflow, with cashflow = sum(dq_i * fill_price_i)
and qty_t = qty_{t-1} + sum(dq_i). The residual (dividends, borrow, fees,
unpriced names) is the equity change minus the sum of every contribution —
reported, never folded into a bucket.

The half-open window [a.ts, b.ts) matters: a snapshot is written at the
START of a run, before that run's own fills are journaled, so a fill
stamped with a run's own timestamp belongs to the interval that ENDS at
the next snapshot, not the one it shares a timestamp with.
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict

SIGN = {"buy": 1.0, "cover": 1.0, "sell": -1.0, "short": -1.0}

# The registration's kill rules are evaluated only once the window has
# enough sessions to say anything — matches
# reports/absolute_return_paper_validation_registration.json's "evaluated
# daily from session 20 onward".
MIN_SESSIONS_FOR_KILL_RULES = 20
PAIRED_BAND_MIN_OBSERVATIONS = 63  # backtest.promotion.paired_drawdown_noise_pp's block_size


def _last_buy_or_short_sleeve(conn: sqlite3.Connection, symbol: str) -> str | None:
    """Most recent buy/short order's sleeve for `symbol`, unbounded by any
    `since` cursor — the only record of what most recently opened or added
    to a position that carries no origin in either snapshot's diag (a
    genuinely orphaned/unattributed dust position, or one predating the
    reporting window). Deliberately duplicates
    `scripts.run_daily.held_sleeve_by_symbol`'s query rather than importing
    it: `engine/` must not depend on `scripts/`'s orchestration-layer
    imports (Trader, argparse, ...) just for this lookup. Keep the two in
    sync if the query ever changes.
    """
    row = conn.execute(
        "SELECT sleeve FROM orders WHERE symbol=? AND side IN ('buy','short') "
        "AND sleeve IS NOT NULL ORDER BY ts DESC LIMIT 1",
        (symbol,),
    ).fetchone()
    return row[0] if row and row[0] else None


def _bucket(sleeve: str | None) -> str:
    """Display bucket for a sleeve-origin string.

    `engine.portfolio` builds `+`-joined combinations (e.g.
    "equity_core+trend"); shown as-is rather than mapped through a
    hardcoded sleeve list, which would go stale every time the portfolio's
    composition changes — mom_ls/tsmom/trend all stood down 2026-09-14/15,
    lev_trend is new, and the next change will rename these again.
    """
    return sleeve if sleeve else "unattributed"


def _max_drawdown(values: list[float]) -> float:
    """Min of value/peak - 1 over the series, peak floored at the first
    value (matches backtest.return_uncertainty_study's convention: a
    first-interval loss should not read as a smaller drawdown just because
    the running peak hasn't caught up yet)."""
    if not values:
        return 0.0
    peak = values[0]
    dd = 0.0
    for v in values:
        peak = max(peak, v)
        dd = min(dd, v / peak - 1.0)
    return dd


def sleeve_pnl(conn: sqlite3.Connection, *, since: str | None = None) -> dict:
    """Daily and cumulative mark-to-market P&L by sleeve, plus the SPY
    buy-and-hold return, daily return series, and max drawdown over the
    identical dates — the SPY mark comes from inside each snapshot's own
    `positions` blob (equity_core always holds SPY), so the benchmark
    needs no separate data source and covers exactly the sessions judged.

    `since`: an ISO timestamp cursor; only snapshots at or after it are
    used, and the first included snapshot is the baseline for both the
    P&L decomposition and the return/drawdown series (its own interval is
    not scored, the same convention `since` cursors use elsewhere in this
    repo). Returns {} if fewer than 2 qualifying snapshots exist.
    """
    query = "SELECT ts, equity, positions, diag FROM snapshots"
    params: tuple = ()
    if since:
        query += " WHERE ts >= ?"
        params = (since,)
    query += " ORDER BY ts"
    snaps = conn.execute(query, params).fetchall()
    if len(snaps) < 2:
        return {}

    first_ts = snaps[0][0]
    fill_rows = conn.execute(
        "SELECT ts, symbol, side, sleeve, filled_qty, filled_avg_price FROM orders "
        "WHERE COALESCE(status,'')='filled' AND filled_qty > 0 "
        "AND filled_avg_price IS NOT NULL AND ts >= ? ORDER BY ts",
        (first_ts,),
    ).fetchall()
    by_ts: dict[str, list[tuple]] = defaultdict(list)
    for ts, symbol, side, sleeve, filled_qty, filled_avg_price in fill_rows:
        by_ts[ts].append((symbol, side, sleeve, filled_qty, filled_avg_price))
    fill_ts_sorted = sorted(by_ts)

    # Fallback sleeve for a symbol with no origin in either snapshot's
    # diag — looked up lazily (and unbounded by `since`) so a position
    # opened before the reporting window still gets its real sleeve
    # instead of reading as unattributed. Cached per symbol since the
    # underlying order history doesn't change mid-run.
    fallback_sleeve_cache: dict[str, str | None] = {}

    def fallback_sleeve(symbol: str) -> str | None:
        if symbol not in fallback_sleeve_cache:
            fallback_sleeve_cache[symbol] = _last_buy_or_short_sleeve(conn, symbol)
        return fallback_sleeve_cache[symbol]

    daily: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    cumulative: dict[str, float] = defaultdict(float)
    portfolio_returns: list[float] = []
    spy_returns: list[float] = []
    last_spy_price: float | None = None
    spy_priced_at_least_once = False

    for i in range(1, len(snaps)):
        a_ts, a_equity, a_positions, a_diag = snaps[i - 1]
        b_ts, b_equity, b_positions, b_diag = snaps[i]
        pos_a = json.loads(a_positions or "{}")
        pos_b = json.loads(b_positions or "{}")
        origin_a = json.loads(a_diag or "{}").get("origin") or {}
        origin_b = json.loads(b_diag or "{}").get("origin") or {}
        px_b = {symbol: float(row["px"]) for symbol, row in pos_b.items()}

        # Always append to both series in lockstep, even on the (never
        # legitimately expected) zero-equity edge case, so the two stay
        # equal length — validation_kill_rule_status requires that before
        # it will even attempt the paired-drawdown band.
        portfolio_returns.append(b_equity / a_equity - 1.0 if a_equity else 0.0)
        spy_price_a = last_spy_price
        if "SPY" in pos_a:
            spy_price_a = float(pos_a["SPY"]["px"])
        spy_price_b = px_b.get("SPY", spy_price_a)
        if spy_price_a and spy_price_b:
            spy_returns.append(spy_price_b / spy_price_a - 1.0)
            spy_priced_at_least_once = True
        else:
            spy_returns.append(0.0)  # no SPY mark yet this interval: not judged
        last_spy_price = spy_price_b

        window = [f for t in fill_ts_sorted if a_ts <= t < b_ts for f in by_ts[t]]
        day = str(b_ts)[:10]
        total = 0.0
        symbols = set(pos_a) | set(pos_b) | {f[0] for f in window}
        for symbol in symbols:
            qty_a = float(pos_a[symbol]["qty"]) if symbol in pos_a else 0.0
            px_a = float(pos_a[symbol]["px"]) if symbol in pos_a else None
            sym_fills = [f for f in window if f[0] == symbol]
            mark_b = px_b.get(symbol)
            if mark_b is None and sym_fills:
                mark_b = float(sym_fills[-1][4])  # last fill price this interval
            sleeve = origin_a.get(symbol) or origin_b.get(symbol) or fallback_sleeve(symbol)
            if px_a is not None and mark_b is not None and qty_a != 0.0:
                pnl = qty_a * (mark_b - px_a)
                bucket = _bucket(sleeve)
                daily[day][bucket] += pnl
                cumulative[bucket] += pnl
                total += pnl
            for _, side, fill_sleeve, filled_qty, filled_avg_price in sym_fills:
                if mark_b is None:
                    continue
                dq = SIGN[side] * float(filled_qty)
                pnl = dq * (mark_b - float(filled_avg_price))
                bucket = _bucket(sleeve or fill_sleeve)
                daily[day][bucket] += pnl
                cumulative[bucket] += pnl
                total += pnl
        residual = (b_equity - a_equity) - total
        daily[day]["residual"] += residual
        cumulative["residual"] += residual

    start_equity, end_equity = snaps[0][1], snaps[-1][1]
    portfolio_return = (end_equity / start_equity - 1.0) if start_equity else None
    spy_equity_curve = [1.0]
    for r in spy_returns:
        spy_equity_curve.append(spy_equity_curve[-1] * (1.0 + r))
    spy_return = (spy_equity_curve[-1] - 1.0) if spy_priced_at_least_once else None

    return {
        "since": snaps[0][0],
        "through": snaps[-1][0],
        "sessions": len(snaps),
        "start_equity": start_equity,
        "end_equity": end_equity,
        "portfolio_return": portfolio_return,
        "spy_return": spy_return,
        "excess_return": (
            portfolio_return - spy_return
            if portfolio_return is not None and spy_return is not None
            else None
        ),
        "cumulative_by_bucket": dict(cumulative),
        "daily_by_bucket": {d: dict(v) for d, v in daily.items()},
        "portfolio_daily_returns": portfolio_returns,
        "spy_daily_returns": spy_returns,
        "portfolio_max_drawdown": _max_drawdown(
            [row[1] for row in snaps]
        ),
        "spy_max_drawdown": _max_drawdown(spy_equity_curve),
    }


def validation_kill_rule_status(
    pnl: dict,
    *,
    slippage_bps: float | None,
    drawdown_limit_pct: float,
    return_floor_pct: float = -4.0,
    slippage_limit_bps: float = 25.0,
    min_sessions: int = MIN_SESSIONS_FOR_KILL_RULES,
) -> dict:
    """Automates kill rules K1-K3 from
    `reports/absolute_return_paper_validation_registration.json`:

    - K1: portfolio return since start minus SPY's < `return_floor_pct` pp.
    - K2: portfolio max drawdown from the window's own peak deeper than
      `drawdown_limit_pct` (10% base / 20% 2x), AND — when there is enough
      history for the paired-bootstrap band
      (`backtest.promotion.paired_drawdown_noise_pp`, `PAIRED_BAND_MIN_OBSERVATIONS`
      daily observations) — the excess over SPY's own drawdown exceeds
      that band. Below that history, only the raw absolute comparison is
      reported, flagged `band_not_computable`, since a noise band from too
      few blocks would be meaningless.
    - K3: mean notional-weighted adverse slippage over the window above
      `slippage_limit_bps`. Computed by the caller via
      `engine.attribution.execution_summary(conn, since=<validation start>)`
      and passed in as `slippage_bps`, since it needs no bucket-level P&L.

    K4 (a lev_trend order rejected for a reason other than the drift band)
    is a qualitative/operational check on the rejections table, not
    automated here — flagged as such in the returned dict.

    Before `min_sessions` sessions, every rule reports `insufficient_history`
    regardless of the raw numbers (which are still returned for visibility)
    since the registration only requires evaluation "daily from session 20
    onward" — a verdict from fewer observations is not a verdict.
    """
    if not pnl:
        return {"status": "insufficient_history", "sessions": 0, "rules": {}}

    sessions = pnl["sessions"]
    evaluable = sessions >= min_sessions

    k1_breach = (
        pnl["excess_return"] is not None
        and pnl["excess_return"] * 100 < return_floor_pct
    )

    band_pp = None
    band_note = (
        f"band_not_computable: fewer than {PAIRED_BAND_MIN_OBSERVATIONS} daily observations"
    )
    port_rets, spy_rets = pnl["portfolio_daily_returns"], pnl["spy_daily_returns"]
    if len(port_rets) >= PAIRED_BAND_MIN_OBSERVATIONS and len(port_rets) == len(spy_rets):
        from backtest.promotion import paired_drawdown_noise_pp

        band_pp = paired_drawdown_noise_pp(spy_rets, port_rets)
        band_note = f"{band_pp:.2f}pp paired-bootstrap band"
    excess_dd_pp = (pnl["portfolio_max_drawdown"] - pnl["spy_max_drawdown"]) * 100
    absolute_dd_breach = pnl["portfolio_max_drawdown"] < -abs(drawdown_limit_pct) / 100
    k2_breach = absolute_dd_breach and (band_pp is None or excess_dd_pp > band_pp)

    k3_breach = slippage_bps is not None and slippage_bps > slippage_limit_bps

    rules = {
        "K1_return_vs_spy": {
            "breached": bool(k1_breach) if evaluable else None,
            "excess_return_pp": (
                round(pnl["excess_return"] * 100, 2) if pnl["excess_return"] is not None else None
            ),
            "floor_pp": return_floor_pct,
        },
        "K2_drawdown_vs_band": {
            "breached": bool(k2_breach) if evaluable else None,
            "portfolio_max_dd_pct": round(pnl["portfolio_max_drawdown"] * 100, 2),
            "spy_max_dd_pct": round(pnl["spy_max_drawdown"] * 100, 2),
            "drawdown_limit_pct": drawdown_limit_pct,
            "excess_dd_pp": round(excess_dd_pp, 2),
            "band_note": band_note,
        },
        "K3_slippage": {
            "breached": bool(k3_breach) if evaluable else None,
            "adverse_slippage_bps": slippage_bps,
            "limit_bps": slippage_limit_bps,
        },
        "K4_gate_defect": {
            "breached": None,
            "note": "not automated — review lev_trend rejections manually",
        },
    }
    breached = [name for name, r in rules.items() if r.get("breached")]
    status = (
        "insufficient_history" if not evaluable
        else f"KILL: {', '.join(breached)}" if breached
        else "OK"
    )
    return {"status": status, "sessions": sessions, "min_sessions": min_sessions, "rules": rules}
