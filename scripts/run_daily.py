"""Daily paper-trading run.

    python -m scripts.run_daily            # normal run
    python -m scripts.run_daily --dry-run  # compute and print, no orders

Flow, all deterministic:
  1. Market clock check (handles holidays and DST — no local calendar math).
  2. Refresh account + positions from Alpaca.
  3. Update risk state (peak / day-start / month-start equity, halt flag).
  4. Software stop check: any held position below its recorded stop -> sell.
  5. Build target weights (clone / tsmom / trend), diff against holdings,
     emit proposals only where drift exceeds the rebalance band.
  6. THE GATE (engine/risk.py) — unchanged from backtesting.
  7. Submit approved orders as marketable DAY limit orders.
  8. Journal everything to state/paper.db; write reports/paper/YYYY-MM-DD.md.

The LLM is nowhere in this file. Its jobs (weekly retro, veto scoring) run in
separate cron entries and cannot touch orders.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import sqlite3
from pathlib import Path
from zoneinfo import ZoneInfo

from engine.attribution import build_exposure_attribution
from engine.config import Config, load_config
from engine.data import REPO_ROOT, atr, load_env
from engine.execute import Trader
from engine.leverage_overlay import apply_target_scale, recommend_leverage
from engine.portfolio import build_targets
from engine.risk import (AccountState, MarketContext, Position, RiskState,
                         SymbolData, _experiment_for_sleeve,
                         compute_experiment_standdowns, evaluate,
                         stop_distance_pct)

ET = ZoneInfo("America/New_York")

PROFILES = {
    # name: (config file, env suffix, state suffix)
    "base": ("config.yaml", "", ""),
    "2x":   ("config_2x.yaml", "_2X", "_2x"),
}
# Set per-run in main() from --profile; module-level so db()/state fns see them.
DB = REPO_ROOT / "state" / "paper.db"
RISK_STATE = REPO_ROOT / "state" / "risk_state.json"
REPORT_DIR = REPO_ROOT / "reports" / "paper"
TERMINAL_ORDER_STATUSES = {
    "filled", "canceled", "expired", "rejected", "replaced", "done_for_day",
}
PROTECTIVE_ORDER_TYPES = {"stop", "stop_limit", "trailing_stop"}


def set_profile(name: str) -> tuple[str, str]:
    """Point DB/state/report paths at the profile and return (config, env suffix)."""
    global DB, RISK_STATE, REPORT_DIR
    cfg_file, env_sfx, state_sfx = PROFILES[name]
    DB = REPO_ROOT / "state" / f"paper{state_sfx}.db"
    RISK_STATE = REPO_ROOT / "state" / f"risk_state{state_sfx}.json"
    REPORT_DIR = REPO_ROOT / "reports" / f"paper{state_sfx}"
    return cfg_file, env_sfx


def append_report(path, day, lines: list[str]) -> None:
    """Add one run's block to the day's report, keeping the earlier runs.

    The daily job fires more than once a session. This used to truncate, so
    the file on disk was always the *last* run — which is reliably the quiet
    one, every order having been placed hours earlier. Days of real trading
    were journalled as "approved 0 | submitted 0".
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    header = "" if path.exists() else f"# Paper {day}\n\n"
    with path.open("a") as fh:
        fh.write(header + "\n".join(lines) + "\n\n")


def db() -> sqlite3.Connection:
    DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB)
    conn.executescript("""
    BEGIN;
    CREATE TABLE IF NOT EXISTS snapshots(
        ts TEXT, equity REAL, cash REAL, positions TEXT, diag TEXT);
    CREATE TABLE IF NOT EXISTS orders(
        ts TEXT, symbol TEXT, side TEXT, sleeve TEXT, qty REAL, notional REAL,
        limit_price REAL, stop_price REAL, reason TEXT, alpaca_id TEXT, status TEXT,
        requested_notional REAL, reference_price REAL, filled_qty REAL,
        filled_avg_price REAL, filled_at TEXT);
    CREATE TABLE IF NOT EXISTS rejections(
        ts TEXT, symbol TEXT, reason TEXT, sleeve TEXT, side TEXT,
        requested_notional REAL);
    CREATE TABLE IF NOT EXISTS stops(
        symbol TEXT PRIMARY KEY, stop_price REAL, entry_price REAL, entry_date TEXT, sleeve TEXT);
    CREATE TABLE IF NOT EXISTS attribution_snapshots(
        ts TEXT, equity REAL,
        target_long REAL, target_short REAL, target_gross REAL,
        actual_long REAL, actual_short REAL, actual_gross REAL,
        target_by_sleeve TEXT, actual_by_sleeve TEXT,
        targets TEXT, actual_weights TEXT, largest_symbol_gaps TEXT);
    CREATE TABLE IF NOT EXISTS leverage_recommendations(
        ts TEXT, profile TEXT, mode TEXT, observations INTEGER,
        target_vol REAL, realized_vol REAL, recommended_scale REAL,
        recommended_leverage REAL, ready INTEGER, reason TEXT);
    """)
    # Existing server journals predate attribution. Additive migrations retain
    # every row and allow a normal pull/upgrade without rebuilding state.
    migrations = {
        "orders": {
            "requested_notional": "REAL",
            "reference_price": "REAL",
            "filled_qty": "REAL",
            "filled_avg_price": "REAL",
            "filled_at": "TEXT",
        },
        "rejections": {
            "sleeve": "TEXT",
            "side": "TEXT",
            "requested_notional": "REAL",
        },
    }
    for table, columns in migrations.items():
        existing = {
            str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")
        }
        for column, kind in columns.items():
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
    return conn


def load_risk_state(equity: float, today: dt.date) -> dict:
    if RISK_STATE.exists():
        st = json.loads(RISK_STATE.read_text())
    else:
        st = {"peak_equity": equity, "month": today.strftime("%Y-%m"),
              "month_start_equity": equity, "day": today.isoformat(),
              "day_start_equity": equity, "recent_losses": {}, "halted": False,
              "experiment_realized_pnl": {}, "experiment_standdowns": []}
    if st["day"] != today.isoformat():
        st["day"], st["day_start_equity"] = today.isoformat(), equity
    if st["month"] != today.strftime("%Y-%m"):
        st["month"], st["month_start_equity"] = today.strftime("%Y-%m"), equity
        if st.get("halted"):
            # Restart after a halt rebases the high-water mark (see backtest
            # engine notes — without this the halt re-fires forever).
            st["peak_equity"] = equity
            st["halted"] = False
    st["peak_equity"] = max(st["peak_equity"], equity)
    return st


def save_risk_state(st: dict) -> None:
    RISK_STATE.write_text(json.dumps(st, indent=2))


def is_protective_order(order: dict) -> bool:
    return str(order.get("type", "")).lower() in PROTECTIVE_ORDER_TYPES


def is_liquidation_order(order: dict) -> bool:
    return str(order.get("client_order_id", "")).startswith("bot-") and str(
        order.get("client_order_id", "")
    ).endswith("-flatten")


def order_client_id(today: dt.date, now_et: dt.datetime, symbol: str, side: str) -> str:
    """client_order_id for a regular (non-flatten) buy/short/sell/cover.

    Includes the run's own HH:MM:SS, not just the date: a bare
    bot-YYYYMMDD-SYMBOL-SIDE collides with itself the moment the same
    symbol+side is legitimately submitted twice in one day — the
    stale-order cancel-and-reprice pass resubmitting the same cover, or
    mom_ls adding to a name it already bought that morning. Alpaca 403/422s
    the resubmission ("client_order_id must be unique") and the order is
    dropped entirely — live on 2026-08-18, this left OLLI (base) and WING
    (2x) both stuck short with every subsequent cover attempt failing the
    same way, never actually closed.

    now_et is fixed once per run (computed at the top of main()), so this
    stays deterministic within a single invocation — the only property the
    original scheme needed — while no longer colliding across separate runs
    on the same day. Unrelated to flatten_id (kill-switch liquidation),
    which stays deliberately date-only: repeated flatten attempts are
    meant to be idempotent, not distinct, retries of the same "make this
    flat" intent — see docs/architecture.md's "idempotent" kill-switch
    note."""
    return f"bot-{today:%Y%m%d}-{now_et:%H%M%S}-{symbol}-{side}"


def marketable_limit(price: float, side: str, slippage: float) -> float:
    """Round toward the touch so cents cannot breach the configured band."""
    if side in ("buy", "cover"):
        return math.floor(price * (1 + slippage) * 100) / 100
    if side in ("sell", "short"):
        return math.ceil(price * (1 - slippage) * 100) / 100
    raise ValueError(f"unsupported side {side!r}")


def broker_fill_fields(order: dict) -> tuple[float | None, float | None, str | None]:
    """Parse optional Alpaca fill fields without turning telemetry into risk."""
    try:
        filled_qty = (
            float(order["filled_qty"])
            if order.get("filled_qty") is not None else None
        )
    except (TypeError, ValueError):
        filled_qty = None
    try:
        filled_avg = (
            float(order["filled_avg_price"])
            if order.get("filled_avg_price") is not None else None
        )
    except (TypeError, ValueError):
        filled_avg = None
    return filled_qty, filled_avg, order.get("filled_at")


def reconcile_journal_orders(conn: sqlite3.Connection, trader: Trader) -> dict[str, int]:
    """Refresh non-terminal journal rows from Alpaca, the source of truth."""
    counts: dict[str, int] = {}
    rows = conn.execute(
        "SELECT DISTINCT alpaca_id FROM orders "
        "WHERE alpaca_id IS NOT NULL AND (status NOT IN "
        "('filled','canceled','expired','rejected','replaced','done_for_day') "
        "OR (status='filled' AND "
        "(filled_qty IS NULL OR filled_avg_price IS NULL)))"
    ).fetchall()
    for (order_id,) in rows:
        try:
            remote = trader.get_order(order_id)
        except Exception as exc:
            print(f"  reconcile {order_id} failed: {exc}")
            continue
        status = str(remote.get("status", "unknown"))
        filled_qty, filled_avg, filled_at = broker_fill_fields(remote)
        conn.execute(
            "UPDATE orders SET status=?, "
            "filled_qty=COALESCE(?, filled_qty), "
            "filled_avg_price=COALESCE(?, filled_avg_price), "
            "filled_at=COALESCE(?, filled_at) WHERE alpaca_id=?",
            (status, filled_qty, filled_avg, filled_at, order_id),
        )
        counts[status] = counts.get(status, 0) + 1
    return counts


def cancel_symbol_orders(
    trader: Trader, symbol: str, open_orders: list[dict], *, dry_run: bool
) -> int:
    """Cancel all live orders for a symbol before intentionally reducing it."""
    matches = [o for o in open_orders if o.get("symbol") == symbol and o.get("id")]
    for order in matches:
        if not dry_run:
            trader.cancel_order(str(order["id"]))
    return len(matches)


# A marketable limit that goes non-marketable right after submission
# (price gapped away) otherwise sits live until the day-TIF close, and —
# worse — its symbol stays in pending_symbols, which blocks every later
# full run from re-pricing it. 2026-08-13: BE and HUT sell limits went
# stale seconds after the 09:51 open run, the 12:39 midday run skipped
# both symbols because of the pending guard, and both orders died
# unfilled at the close (HUT drifted ~6% below its limit meanwhile).
# 30 minutes is comfortably longer than any observed legitimate fill
# latency (worst seen: ~15 min on a wide-spread name).
STALE_ORDER_CANCEL_MINUTES = 30


def stale_pending_orders(
    open_orders: list[dict],
    now: dt.datetime,
    threshold_minutes: float = STALE_ORDER_CANCEL_MINUTES,
) -> list[tuple[dict, float]]:
    """Non-protective open orders older than the threshold, with ages in
    minutes. Protective (stop) orders are meant to rest indefinitely and
    are never considered stale; orders with an unparsable submitted_at
    are skipped rather than guessed at."""
    stale = []
    for order in open_orders:
        if is_protective_order(order):
            continue
        submitted = order.get("submitted_at") or order.get("created_at")
        try:
            ts = dt.datetime.fromisoformat(str(submitted).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=dt.timezone.utc)
        age_minutes = (now - ts).total_seconds() / 60
        if age_minutes >= threshold_minutes:
            stale.append((order, age_minutes))
    return stale


def sync_broker_stops(
    conn: sqlite3.Connection,
    positions: dict[str, Position],
    open_orders: list[dict],
    today: dt.date,
) -> set[str]:
    """Mirror broker-held protective stops into the software fallback table."""
    protective_symbols: set[str] = set()
    by_symbol: dict[str, list[float]] = {}
    for order in open_orders:
        if not is_protective_order(order):
            continue
        symbol = str(order.get("symbol", ""))
        try:
            stop = float(order.get("stop_price"))
        except (TypeError, ValueError):
            continue
        if symbol in positions and stop > 0:
            protective_symbols.add(symbol)
            by_symbol.setdefault(symbol, []).append(stop)

    for symbol, stops in by_symbol.items():
        pos = positions[symbol]
        # Multiple OTO additions can create multiple child stops. For the
        # fallback monitor use the most protective valid boundary.
        stop = min(stops) if pos.is_short else max(stops)
        conn.execute(
            "INSERT OR REPLACE INTO stops VALUES (?,?,?,?,?)",
            (symbol, stop, pos.avg_entry_price, today.isoformat(), "broker"),
        )
    pending_entry_symbols = {
        str(order.get("symbol"))
        for order in open_orders
        if order.get("symbol") and not is_protective_order(order)
    }
    for (symbol,) in conn.execute("SELECT symbol FROM stops").fetchall():
        if symbol not in positions and symbol not in pending_entry_symbols:
            conn.execute("DELETE FROM stops WHERE symbol=?", (symbol,))
    return protective_symbols


def backfill_missing_stops(
    conn: sqlite3.Connection,
    trader: Trader,
    cfg: Config,
    positions: dict[str, Position],
    raw_positions: list[dict],
    held_sleeve: dict[str, str],
    today: dt.date,
) -> list[str]:
    """Establish a fallback stop for any held, non-exempt position that has
    never had one — neither a live broker stop (sync_broker_stops above
    mirrors those into `stops` already) nor a previously-recorded fallback
    row.

    Nothing else revisits an existing position to check this: a fallback
    stop is normally only written at entry time (submit_entry's fractional
    path) or mirrored from a currently-open broker stop order. An entry
    whose original order got neither — predates that fractional-entry
    fallback path, or hit some other silent gap — stays unprotected
    indefinitely. Live on 2x: GLD's fractional buy on 2026-07-23 got
    neither and sat unprotected for a month before
    scripts/healthcheck.py's "no broker or fallback stop" alert caught it
    on 2026-08-20.
    """
    stopped = {r[0] for r in conn.execute("SELECT symbol FROM stops")}
    asset_class = {
        str(p.get("symbol")): str(p.get("asset_class", "")) for p in raw_positions
    }
    candidates = sorted(
        symbol for symbol, pos in positions.items()
        if symbol not in stopped
        and pos.current_price > 0
        # Options legs are defined-risk by the spread's own maximum_loss,
        # never an equity-style stop — see scripts/healthcheck.py's
        # matching us_option exclusion for why flagging them is noise.
        and asset_class.get(symbol) != "us_option"
        and cfg.risk.stops_apply_to(held_sleeve.get(symbol, ""))
    )
    if not candidates:
        return []
    history_days = max(400, cfg.universe.exclude_ipo_days * 2 + 40)
    bars = trader.get_bars(candidates, today - dt.timedelta(days=history_days), today)
    backfilled = []
    for symbol in candidates:
        pos = positions[symbol]
        df = bars.get(symbol)
        atr14 = (
            float(atr(df, 14).iloc[-1]) if df is not None and len(df) >= 15
            else pos.current_price * 0.02
        )
        stop_pct = stop_distance_pct(cfg, pos.current_price, atr14)
        stop_price = round(
            pos.current_price * (1 + stop_pct if pos.is_short else 1 - stop_pct), 4
        )
        if stop_price <= 0:
            continue
        conn.execute(
            "INSERT OR REPLACE INTO stops VALUES (?,?,?,?,?)",
            (symbol, stop_price, pos.avg_entry_price, today.isoformat(), "backfill"),
        )
        backfilled.append(symbol)
    return backfilled


def held_sleeve_by_symbol(conn: sqlite3.Connection, symbols) -> dict[str, str]:
    """Best-known sleeve attribution for each currently-held symbol.

    Positions carry no sleeve tag from the broker — the journal's most
    recent buy/short order for a symbol is the only record of which sleeve
    most recently opened or added to it. A symbol with no such order (e.g.
    a position opened before the orders table existed, or outside the bot)
    maps to nothing and is therefore not attributed to any experiment.
    """
    out: dict[str, str] = {}
    for symbol in symbols:
        row = conn.execute(
            "SELECT sleeve FROM orders WHERE symbol=? AND side IN ('buy','short') "
            "AND sleeve IS NOT NULL ORDER BY ts DESC LIMIT 1",
            (symbol,),
        ).fetchone()
        if row and row[0]:
            out[symbol] = row[0]
    return out


def experiment_exposure_and_unrealized_pnl(
    positions: dict[str, Position], held_sleeve: dict[str, str], cfg: Config,
) -> tuple[dict[str, float], dict[str, float]]:
    """Dollar gross exposure and unrealized P&L per experiment name, from
    currently held positions and their journal-derived sleeve attribution.

    Pure — no I/O — so the aggregation is unit-testable without a database.
    Unrealized P&L uses each position's overall unrealized_pct (entry to
    current price), the same precision the pre-existing recent_losses check
    already relies on — not exact per-lot cost basis.
    """
    exposure: dict[str, float] = {}
    unrealized: dict[str, float] = {}
    if not cfg.experiments:
        return exposure, unrealized
    for symbol, sleeve in held_sleeve.items():
        pos = positions.get(symbol)
        if pos is None:
            continue
        experiment = _experiment_for_sleeve(cfg, sleeve)
        if experiment is None:
            continue
        exposure[experiment.name] = exposure.get(experiment.name, 0.0) + abs(pos.market_value)
        unrealized[experiment.name] = (
            unrealized.get(experiment.name, 0.0) + pos.unrealized_pct * abs(pos.market_value)
        )
    return exposure, unrealized


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="run even when market is closed (for testing)")
    ap.add_argument("--profile", choices=list(PROFILES), default="base")
    ap.add_argument(
        "--stops-only", action="store_true",
        help="skip target computation and rebalance drift; only check software stops",
    )
    args = ap.parse_args()

    cfg_file, env_sfx = set_profile(args.profile)
    cfg = load_config(REPO_ROOT / cfg_file)

    load_env()
    key = os.environ.get(f"ALPACA_API_KEY{env_sfx}")
    secret = os.environ.get(f"ALPACA_API_SECRET{env_sfx}")
    if env_sfx and not (key and secret):
        print(f"CRITICAL: profile {args.profile} needs ALPACA_API_KEY{env_sfx} / "
              f"ALPACA_API_SECRET{env_sfx} in .env — standing down")
        return
    t = Trader(key=key, secret=secret) if env_sfx else Trader()
    print(f"profile={args.profile} config={cfg_file} lev={cfg.sleeves_paper.get('gross_leverage', 1.0)}")
    clock = t.clock()
    now_et = dt.datetime.now(ET)
    today = now_et.date()
    print(f"[{now_et:%Y-%m-%d %H:%M ET}] market_open={clock.get('is_open')}")

    if not clock.get("is_open") and not (args.dry_run or args.force):
        print("market closed — exiting")
        return

    account = t.get_account()
    equity, cash = float(account["equity"]), float(account["cash"])
    shorting_enabled = account.get("shorting_enabled") is True or str(
        account.get("shorting_enabled", "")
    ).lower() == "true"
    raw_positions = t.get_positions()
    positions = {
        p["symbol"]: Position(p["symbol"], float(p["qty"]),
                              float(p["avg_entry_price"]), float(p["current_price"]))
        for p in raw_positions
    }
    print(f"equity ${equity:,.2f}  cash ${cash:,.2f}  positions {len(positions)}")

    st = load_risk_state(equity, today)
    conn = db()
    ts = now_et.isoformat()

    # ---- experiment-tier exposure / P&L / stand-down -----------------
    # held_sleeve is computed unconditionally — the experiment aggregation
    # below is a no-op without cfg.experiments, but the stop-backfill step
    # further down (also independent of halt/--stops-only mode) needs it
    # for every profile to know which held positions are stop-exempt.
    held_sleeve = held_sleeve_by_symbol(conn, positions)
    exp_exposure, exp_unrealized = experiment_exposure_and_unrealized_pnl(
        positions, held_sleeve, cfg
    )
    exp_realized = dict(st.get("experiment_realized_pnl", {}))
    cumulative_pnl_pct = {
        name: (exp_realized.get(name, 0.0) + exp_unrealized.get(name, 0.0)) / equity
        for name in cfg.experiments
    } if equity > 0 else {}
    experiment_standdowns = compute_experiment_standdowns(cfg, cumulative_pnl_pct)
    previously_stood_down = set(st.get("experiment_standdowns", []))
    newly_stood_down = experiment_standdowns - previously_stood_down
    if experiment_standdowns:
        print(f"experiments: standdown={sorted(experiment_standdowns)} "
              f"new={sorted(newly_stood_down)}")

    fixed_leverage = float(cfg.sleeves_paper.get("gross_leverage", 1.0))
    overlay = recommend_leverage(
        conn,
        current_ts=ts,
        current_equity=equity,
        fixed_leverage=fixed_leverage,
        settings=cfg.sleeves_paper.get(
            "volatility_overlay", {"mode": "off"}
        ),
    )
    if overlay["mode"] != "off":
        vol_text = (
            f"{overlay['realized_vol']:.1%}"
            if overlay["realized_vol"] is not None else "pending"
        )
        print(
            f"volatility overlay={overlay['mode']} "
            f"observations={overlay['observations']}/"
            f"{overlay['min_observations']} realized={vol_text} "
            f"recommended={overlay['recommended_leverage']:.2f}x "
            f"applied={overlay['applied_leverage']:.2f}x"
        )
    reconciled = reconcile_journal_orders(conn, t)
    open_orders = t.open_orders()
    protective_symbols = sync_broker_stops(conn, positions, open_orders, today)
    backfilled_stops = backfill_missing_stops(
        conn, t, cfg, positions, raw_positions, held_sleeve, today
    )
    if backfilled_stops:
        print(f"  backfilled fallback stop for {', '.join(backfilled_stops)} "
              "(held with neither a broker nor software stop)")
    pending_symbols = {
        str(o.get("symbol")) for o in open_orders
        if o.get("symbol") and not is_protective_order(o)
    }

    # Stale-order cancel & re-price — full runs only: the drift loop below
    # re-proposes the freed symbol at a fresh marketable price in this
    # same run, which is the entire point. stops-only runs skip the drift
    # loop, so canceling there would orphan the intent until the next
    # full run — strictly worse than leaving the order resting.
    if not args.stops_only:
        for order, age_minutes in stale_pending_orders(
            open_orders, dt.datetime.now(dt.timezone.utc)
        ):
            symbol = str(order.get("symbol"))
            print(f"  cancel stale {order.get('side')} {symbol} "
                  f"(unfilled {age_minutes:.0f}m, limit {order.get('limit_price')}) "
                  "— will re-price this run")
            if not args.dry_run:
                try:
                    t.cancel_order(str(order["id"]))
                except Exception as exc:
                    print(f"  cancel {order.get('id')} failed: {exc} — leaving pending")
                    continue
                # Mark the journal row now rather than waiting a full run
                # for reconcile to notice (the dashboard's stuck-order
                # signal reads this status).
                conn.execute(
                    "UPDATE orders SET status='canceled' WHERE alpaca_id=?",
                    (str(order["id"]),),
                )
            # Freed for the drift loop (in dry runs too, so the dry run
            # previews exactly what a real run would re-propose).
            pending_symbols.discard(symbol)

    if reconciled or open_orders:
        print(f"orders: reconciled={reconciled} open={len(open_orders)} "
              f"pending_symbols={len(pending_symbols)} stops={len(protective_symbols)}")

    # ---- targets ---------------------------------------------------------
    # Halt mode must not depend on research-data availability. It exists to
    # cancel orders and flatten risk even if target construction is broken.
    # --stops-only shares the empty-targets placeholder for the same reason
    # halt mode does (skip the expensive bar fetch in build_targets), but —
    # unlike halt mode — nothing downstream may treat these empty targets as
    # "flatten everything": halt mode's flattening comes from the gate's own
    # cfg.mode=="halt" check (engine/risk.py) via result.flatten_all, not
    # from the drift loop below reading a zero target. --stops-only runs
    # under the normal mode, so section 4b (rebalance drift) is explicitly
    # skipped rather than fed these placeholder targets — see there.
    if cfg.mode == "halt" or args.stops_only:
        targets = {}
        diag = {
            "sleeve_counts": {}, "total_weight": 0.0, "cash_weight": 1.0,
            "origin": {}, "gross_leverage": 0.0, "sleeve_targets": {},
        }
    else:
        targets, diag = build_targets(cfg, t, held=frozenset(positions))
        targets, diag = apply_target_scale(targets, diag, overlay)
    diag["volatility_overlay"] = overlay
    if args.stops_only:
        diag["stops_only"] = True
    print(f"targets: {diag['sleeve_counts']}  total={diag['total_weight']}  cash={diag['cash_weight']}")

    # ---- proposals -------------------------------------------------------
    p = cfg.sleeves_paper
    band, min_notional = float(p["rebalance_band"]), float(p["min_order_notional"])
    proposals = []
    # The gate must validate against the same quote snapshot used to construct
    # each limit. A second live quote can move by a cent between API calls and
    # make an exactly-on-policy order appear outside the slippage band.
    reference_prices: dict[str, float] = {}

    # 4a. software stops — always first, always sells
    stop_rows = {r[0]: r[1] for r in conn.execute("SELECT symbol, stop_price FROM stops")}
    for sym, pos in positions.items():
        if sym in protective_symbols:
            continue  # broker owns the active stop; do not race it
        stop = stop_rows.get(sym)
        if not stop:
            continue
        if pos.is_short and pos.current_price >= stop:
            reference_prices[sym] = pos.current_price
            proposals.append({"symbol": sym, "side": "cover", "sleeve": "stop",
                              "notional": abs(pos.market_value),
                              "limit_price": marketable_limit(
                                  pos.current_price, "cover",
                                  cfg.execution.max_limit_slippage_pct,
                              ),
                              "rationale": f"short stop: {pos.current_price} >= {stop}"})
        elif not pos.is_short and pos.current_price <= stop:
            reference_prices[sym] = pos.current_price
            proposals.append({"symbol": sym, "side": "sell", "sleeve": "stop",
                              "notional": pos.market_value,
                              "limit_price": marketable_limit(
                                  pos.current_price, "sell",
                                  cfg.execution.max_limit_slippage_pct,
                              ),
                              "rationale": f"software stop: {pos.current_price} <= {stop}"})

    # 4a-bis. experiment stand-down flatten — a newly-triggered stand-down
    # must not leave existing sleeve risk in place until the next signal
    # happens to reduce it. Generated immediately, same priority as a
    # software stop, and (like 4a) runs even in --stops-only mode since
    # this is a risk-reduction action, not a rebalance.
    for name in sorted(newly_stood_down):
        for sym, sleeve in held_sleeve.items():
            experiment = _experiment_for_sleeve(cfg, sleeve)
            if experiment is None or experiment.name != name:
                continue
            if any(pr["symbol"] == sym for pr in proposals):
                continue
            pos = positions.get(sym)
            if pos is None:
                continue
            reference_prices[sym] = pos.current_price
            side = "cover" if pos.is_short else "sell"
            proposals.append({
                "symbol": sym, "side": side, "sleeve": sleeve,
                "notional": abs(pos.market_value),
                "limit_price": marketable_limit(
                    pos.current_price, side, cfg.execution.max_limit_slippage_pct,
                ),
                "rationale": f"experiment {name!r} stood down: cumulative loss limit breached",
            })

    # 4b. rebalance drift — skipped entirely in --stops-only mode. targets is
    # a placeholder ({}) there, and letting it reach this loop would read as
    # "every held position's target is zero" and propose liquidating the
    # whole book — see the note above where targets is set.
    if not args.stops_only:
        all_syms = sorted(set(targets) | set(positions))
        for sym in all_syms:
            if sym in pending_symbols:
                continue
            if any(pr["symbol"] == sym for pr in proposals):
                continue
            tgt_notional = targets.get(sym, 0.0) * equity        # negative = short target
            cur_notional = positions[sym].market_value if sym in positions else 0.0
            diff = tgt_notional - cur_notional
            threshold = max(band * abs(tgt_notional), min_notional) if tgt_notional != 0 else min_notional
            if abs(diff) < threshold:
                continue
            px = t.latest_price(sym)
            if px is None or px <= 0:
                continue
            reference_prices[sym] = px

            # Map the sign geometry to a side. Crossing zero (long target while
            # short, or vice versa) is handled by closing first; the opening leg
            # happens on a later run once flat. Never both in one order.
            if diff > 0:
                side = "cover" if cur_notional < 0 else "buy"
                if side == "cover":
                    diff = min(diff, -cur_notional)              # close at most the short
            else:
                side = "sell" if cur_notional > 0 else "short"
                if side == "sell":
                    diff = max(diff, -cur_notional)              # sell at most what we hold
            if abs(diff) < min_notional:
                continue
            limit = marketable_limit(
                px, side, cfg.execution.max_limit_slippage_pct
            )
            proposals.append({"symbol": sym, "side": side,
                              "sleeve": diag["origin"].get(sym, "rebalance"),
                              "notional": abs(diff), "limit_price": limit,
                              "rationale": f"target ${tgt_notional:,.0f} vs held ${cur_notional:,.0f}"})

    print(f"proposals: {len(proposals)}")

    if args.stops_only and not proposals:
        # True no-op for the SNAPSHOT: no stop triggered, so no misleading
        # placeholder-target row. But reconcile_journal_orders/
        # sync_broker_stops above may have made real, legitimate writes
        # (mirroring a broker-side fill or a pruned stale stop row) that
        # share this same connection and are not yet committed — nothing
        # else in this function will commit them if we return now, so they
        # would silently roll back otherwise. Commit just that housekeeping.
        if not args.dry_run:
            conn.commit()
        print("done: stops-only, nothing triggered")
        return

    # ---- gate ------------------------------------------------------------
    symbol_data = {}
    if proposals:
        need = sorted({pr["symbol"] for pr in proposals})
        # Alpaca's asset endpoint carries no listing/IPO date, so listed_days
        # is proxied from how far back bar history actually goes — the same
        # technique scripts/weekly.py already relies on for universe history
        # length. The window has to comfortably clear exclude_ipo_days or an
        # old symbol looks artificially "recent" just because the fetch
        # didn't reach back far enough; a name genuinely newer than the
        # window is unaffected since we only need to resolve the threshold,
        # not the exact listing date.
        history_days = max(400, cfg.universe.exclude_ipo_days * 2 + 40)
        bars = t.get_bars(need, today - dt.timedelta(days=history_days), today)
        for sym in need:
            df = bars.get(sym)
            px = reference_prices.get(sym) or (
                float(df["close"].iloc[-1])
                if df is not None and len(df)
                else 0.0
            )
            a = float(atr(df, 14).iloc[-1]) if df is not None and len(df) >= 15 else px * 0.02
            adv = float((df["close"] * df["volume"]).tail(20).mean()) if df is not None and len(df) >= 20 else 0.0
            # No bars at all is treated as unlisted/unknown (0 days), not as
            # "old enough" — a name with no history shouldn't clear the IPO
            # gate by default, and the liquidity gate rejects it anyway.
            listed_days = (
                (today - df.index[0].date()).days if df is not None and len(df) else 0
            )
            shortable = False
            if any(pr["symbol"] == sym and pr["side"] == "short" for pr in proposals):
                try:
                    asset = t.get_asset(sym)
                    shortable = bool(asset.get("shortable") and asset.get("easy_to_borrow"))
                except Exception:
                    shortable = False
            symbol_data[sym] = SymbolData(price=px, atr14=a, avg_dollar_volume_20d=adv,
                                          listed_days=listed_days,
                                          is_leveraged=sym in cfg.leveraged_symbols,
                                          shortable=shortable)

    risk_state = RiskState(
        peak_equity=st["peak_equity"], day_start_equity=st["day_start_equity"],
        month_start_equity=st["month_start_equity"],
        recent_losses={k: dt.date.fromisoformat(v) for k, v in st["recent_losses"].items()},
        halted=st["halted"],
        experiment_standdowns=experiment_standdowns,
    )
    ctx = MarketContext(now=now_et, is_trading_day=bool(clock.get("is_open")) or args.force,
                        symbols=symbol_data)
    lev = float(overlay["applied_leverage"])
    buying_power = None
    if lev > 1.0:
        long_exposure = sum(p2.market_value for p2 in positions.values() if not p2.is_short)
        buying_power = max(lev * equity - long_exposure, 0.0)
    result = evaluate(proposals, AccountState(equity=equity, cash=cash, positions=positions,
                                              buying_power=buying_power,
                                              shorting_enabled=shorting_enabled,
                                              experiment_gross_exposure=exp_exposure),
                      risk_state, ctx, cfg)

    for rej in result.rejected:
        raw = rej.raw if isinstance(rej.raw, dict) else {}
        conn.execute(
            "INSERT INTO rejections("
            "ts, symbol, reason, sleeve, side, requested_notional"
            ") VALUES (?,?,?,?,?,?)",
            (
                ts,
                rej.symbol,
                rej.reason,
                raw.get("sleeve"),
                raw.get("side"),
                raw.get("notional"),
            ),
        )
        print(f"  REJECT {rej.symbol}: {rej.reason}")
    if result.halt_reason:
        st["halted"] = True
        print(f"  HALT: {result.halt_reason}")
    if result.flatten_all and not args.dry_run:
        print("  FLATTEN: selling everything per kill switch")
        existing_liquidations = {
            str(o.get("symbol")) for o in open_orders if is_liquidation_order(o)
        }
        # Preserve already-live flatten orders; cancel entries and protective
        # children so they cannot fight the liquidation.
        for live_order in open_orders:
            if not is_liquidation_order(live_order) and live_order.get("id"):
                t.cancel_order(str(live_order["id"]))
        for sym, pos in positions.items():
            if sym in existing_liquidations:
                print(f"    flatten {sym} already open — leaving it in place")
                continue
            px = t.latest_price(sym) or pos.current_price
            try:
                flatten_id = f"bot-{today:%Y%m%d}-{sym}-flatten"
                if pos.is_short:
                    o = t.submit_limit(
                        sym, "buy", abs(pos.qty), round(px * 1.003, 2),
                        client_order_id=flatten_id,
                    )
                    flatten_side = "cover"
                else:
                    o = t.submit_limit(
                        sym, "sell", pos.qty, round(px * 0.997, 2),
                        client_order_id=flatten_id,
                    )
                    flatten_side = "sell"
                filled_qty, filled_avg, filled_at = broker_fill_fields(o)
                conn.execute(
                    "INSERT INTO orders("
                    "ts, symbol, side, sleeve, qty, notional, limit_price, "
                    "stop_price, reason, alpaca_id, status, requested_notional, "
                    "reference_price, filled_qty, filled_avg_price, filled_at"
                    ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        ts, sym, flatten_side, "killswitch", abs(pos.qty),
                        abs(pos.market_value), px, 0.0,
                        result.halt_reason or "flatten", o.get("id"),
                        o.get("status"), abs(pos.market_value), px, filled_qty,
                        filled_avg, filled_at,
                    ),
                )
            except Exception as exc:
                print(f"    flatten {sym} failed: {exc}")

    # ---- execute ---------------------------------------------------------
    submitted = 0
    submission_failures: list[str] = []
    succeeded_orders: list = []
    orders_to_execute = [] if result.flatten_all else result.approved
    for order in orders_to_execute:
        tag = " ".join(order.adjustments) or "clean"
        print(f"  APPROVED {order.side:4} {order.symbol:6} ${order.notional:>9,.2f} "
              f"@{order.limit_price} stop={order.stop_price} [{tag}]")
        if args.dry_run:
            continue
        try:
            # Alpaca has no "short"/"cover" sides: sell-when-flat opens a
            # short, buy-when-short covers.
            alpaca_side = {"short": "sell", "cover": "buy"}.get(order.side, order.side)
            order_id = order_client_id(today, now_et, order.symbol, order.side)
            if order.side in ("buy", "short"):
                o, broker_protected = t.submit_entry(
                    order.symbol, alpaca_side, order.qty, order.limit_price,
                    order.stop_price, client_order_id=order_id,
                )
                if not broker_protected and order.stop_price:
                    # Alpaca permits fractional entries only as simple orders.
                    # Persist the stop before committing the order journal so a
                    # later fill is protected by the software monitor.
                    # A zero stop means the sleeve is deliberately unstopped;
                    # writing the row would claim protection that is not there
                    # and mask it from the healthcheck.
                    conn.execute(
                        "INSERT OR REPLACE INTO stops VALUES (?,?,?,?,?)",
                        (
                            order.symbol,
                            order.stop_price,
                            order.limit_price,
                            today.isoformat(),
                            "fractional-entry",
                        ),
                    )
                    print(
                        f"    {order.symbol}: fractional simple order; "
                        "software fallback stop recorded"
                    )
            else:
                canceled = cancel_symbol_orders(
                    t, order.symbol, open_orders, dry_run=False
                )
                if canceled:
                    print(f"    canceled {canceled} existing {order.symbol} order(s) before exit")
                o = t.submit_limit(
                    order.symbol, alpaca_side, order.qty, order.limit_price,
                    client_order_id=order_id,
                )
            filled_qty, filled_avg, filled_at = broker_fill_fields(o)
            conn.execute(
                "INSERT INTO orders("
                "ts, symbol, side, sleeve, qty, notional, limit_price, "
                "stop_price, reason, alpaca_id, status, requested_notional, "
                "reference_price, filled_qty, filled_avg_price, filled_at"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    ts, order.symbol, order.side, order.sleeve, order.qty,
                    order.notional, order.limit_price, order.stop_price, tag,
                    o.get("id"), o.get("status"), order.requested_notional,
                    reference_prices.get(order.symbol), filled_qty, filled_avg,
                    filled_at,
                ),
            )
            if order.side not in ("buy", "short"):
                conn.execute("DELETE FROM stops WHERE symbol=?", (order.symbol,))
            submitted += 1
            succeeded_orders.append(order)
        except Exception as exc:
            print(f"    submit {order.symbol} FAILED: {exc}")
            submission_failures.append(f"{order.symbol}: {exc}")

    # record realised losses for the revenge-trade block, and realized P&L
    # for any experiment-tier sleeve exit — gains and losses both, since a
    # stand-down is judged on cumulative P&L, not just on losing trades.
    # Iterates succeeded_orders, not result.approved: the gate approving a
    # proposal only means it's allowed, not that the broker accepted it —
    # a submission failure above (client_order_id collision, a rejected
    # notional, a network error) must not still be recorded as a realized
    # loss / experiment P&L event for a position that never actually
    # closed. Live on 2026-08-18, OLLI's cover failed to submit but still
    # landed in recent_losses as if it had exited, which would have wrongly
    # blocked a future re-entry for a position that was — and still is —
    # open.
    experiment_realized_pnl = dict(st.get("experiment_realized_pnl", {}))
    for order in succeeded_orders:
        if order.side in ("sell", "cover") and order.symbol in positions:
            pos = positions[order.symbol]
            if pos.unrealized_pct < 0:
                st["recent_losses"][order.symbol] = today.isoformat()
            experiment = _experiment_for_sleeve(cfg, order.sleeve)
            if experiment is not None:
                # Approximation: the position's overall unrealized_pct
                # (entry to current price) applied to the dollar amount
                # this order closes — not exact per-lot cost basis, the
                # same precision the recent_losses check above already
                # relies on for this same position.
                experiment_realized_pnl[experiment.name] = (
                    experiment_realized_pnl.get(experiment.name, 0.0)
                    + pos.unrealized_pct * order.notional
                )
    st["experiment_realized_pnl"] = experiment_realized_pnl
    st["experiment_standdowns"] = sorted(experiment_standdowns)

    # ---- journal + report ------------------------------------------------
    if args.dry_run:
        # Dry runs may read the real journal for stop/reconciliation context,
        # but must not make a healthy-looking snapshot, rewrite tracked
        # reports, or otherwise hide a stale production cron job.
        conn.rollback()
        print("done: dry run; no orders or local state changes")
        return

    conn.execute("INSERT INTO snapshots VALUES (?,?,?,?,?)",
                 (ts, equity, cash,
                  json.dumps({s: {"qty": p.qty, "px": p.current_price} for s, p in positions.items()}),
                  json.dumps(diag)))
    # attribution_snapshots and leverage_recommendations both describe
    # rebalance-cycle target state (diag["sleeve_targets"], the overlay's
    # recommendation), neither of which --stops-only computes — targets is
    # the {} placeholder here. Writing them anyway would silently claim the
    # book's target exposure is zero, which is the same class of misleading
    # journal row already fixed once for this repo (the truncated-report /
    # 4x-daily-run confusion documented in AGENTS.md).
    if not args.stops_only:
        attribution = build_exposure_attribution(
            equity=equity,
            targets=targets,
            sleeve_targets=diag.get("sleeve_targets", {}),
            positions=positions,
        )
        target_exp = attribution["target"]
        actual_exp = attribution["actual"]
        conn.execute(
            "INSERT INTO attribution_snapshots VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                ts,
                equity,
                target_exp["long"],
                target_exp["short"],
                target_exp["gross"],
                actual_exp["long"],
                actual_exp["short"],
                actual_exp["gross"],
                json.dumps(attribution["target_by_sleeve"]),
                json.dumps(attribution["actual_by_sleeve"]),
                json.dumps(attribution["targets"]),
                json.dumps(attribution["actual_weights"]),
                json.dumps(attribution["largest_symbol_gaps"]),
            ),
        )
        conn.execute(
            "INSERT INTO leverage_recommendations VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                ts,
                args.profile,
                overlay["mode"],
                overlay["observations"],
                overlay["target_vol"],
                overlay["realized_vol"],
                overlay["recommended_scale"],
                overlay["recommended_leverage"],
                int(overlay["ready"]),
                overlay["reason"],
            ),
        )
    conn.commit()
    save_risk_state(st)

    lines = [
        f"## run {ts}" + (" (stops-only)" if args.stops_only else ""),
        f"- equity ${equity:,.2f} | cash ${cash:,.2f} | positions {len(positions)}",
        f"- proposals {len(proposals)} | approved {len(result.approved)} | rejected {len(result.rejected)} | submitted {submitted}",
        f"- submission failures {len(submission_failures)}",
        f"- broker orders open {len(open_orders)} | journal reconciled {sum(reconciled.values())}",
    ]
    if not args.stops_only:
        lines.insert(
            2,
            f"- sleeves {diag['sleeve_counts']} | invested {diag['total_weight']:.0%} | cash target {diag['cash_weight']:.0%}",
        )
        lines += [
            f"- target exposure long {target_exp['long']:.1%} | short {target_exp['short']:.1%} | "
            f"gross {target_exp['gross']:.1%}",
            f"- actual exposure long {actual_exp['long']:.1%} | short {actual_exp['short']:.1%} | "
            f"gross {actual_exp['gross']:.1%}",
            f"- volatility overlay {overlay['mode']} | observations "
            f"{overlay['observations']}/{overlay['min_observations']} | "
            f"recommended {overlay['recommended_leverage']:.2f}× | "
            f"traded {overlay['applied_leverage']:.2f}×",
        ]
    if result.halt_reason:
        lines.append(f"- **HALT: {result.halt_reason}**")
    for n in result.notes:
        lines.append(f"- note: {n}")
    append_report(REPORT_DIR / f"{today}.md", today, lines)
    print(f"done: {submitted} orders submitted")
    if submission_failures:
        print(f"CRITICAL: {len(submission_failures)} broker submission(s) failed")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
