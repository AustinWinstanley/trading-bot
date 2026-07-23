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
import os
import sqlite3
from pathlib import Path
from zoneinfo import ZoneInfo

from engine.config import load_config
from engine.data import REPO_ROOT, atr
from engine.execute import Trader
from engine.portfolio import build_targets
from engine.risk import (AccountState, MarketContext, Position, RiskState,
                         SymbolData, evaluate)

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


def set_profile(name: str) -> tuple[str, str]:
    """Point DB/state/report paths at the profile and return (config, env suffix)."""
    global DB, RISK_STATE, REPORT_DIR
    cfg_file, env_sfx, state_sfx = PROFILES[name]
    DB = REPO_ROOT / "state" / f"paper{state_sfx}.db"
    RISK_STATE = REPO_ROOT / "state" / f"risk_state{state_sfx}.json"
    REPORT_DIR = REPO_ROOT / "reports" / f"paper{state_sfx}"
    return cfg_file, env_sfx


def db() -> sqlite3.Connection:
    DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB)
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS snapshots(
        ts TEXT, equity REAL, cash REAL, positions TEXT, diag TEXT);
    CREATE TABLE IF NOT EXISTS orders(
        ts TEXT, symbol TEXT, side TEXT, sleeve TEXT, qty REAL, notional REAL,
        limit_price REAL, stop_price REAL, reason TEXT, alpaca_id TEXT, status TEXT);
    CREATE TABLE IF NOT EXISTS rejections(
        ts TEXT, symbol TEXT, reason TEXT);
    CREATE TABLE IF NOT EXISTS stops(
        symbol TEXT PRIMARY KEY, stop_price REAL, entry_price REAL, entry_date TEXT, sleeve TEXT);
    """)
    return conn


def load_risk_state(equity: float, today: dt.date) -> dict:
    if RISK_STATE.exists():
        st = json.loads(RISK_STATE.read_text())
    else:
        st = {"peak_equity": equity, "month": today.strftime("%Y-%m"),
              "month_start_equity": equity, "day": today.isoformat(),
              "day_start_equity": equity, "recent_losses": {}, "halted": False}
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


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="run even when market is closed (for testing)")
    ap.add_argument("--profile", choices=list(PROFILES), default="base")
    args = ap.parse_args()

    cfg_file, env_sfx = set_profile(args.profile)
    cfg = load_config(REPO_ROOT / cfg_file)
    if cfg.mode == "halt":
        print("mode=halt — refusing to run")
        return

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
    raw_positions = t.get_positions()
    positions = {
        p["symbol"]: Position(p["symbol"], float(p["qty"]),
                              float(p["avg_entry_price"]), float(p["current_price"]))
        for p in raw_positions
    }
    print(f"equity ${equity:,.2f}  cash ${cash:,.2f}  positions {len(positions)}")

    st = load_risk_state(equity, today)
    conn = db()

    # ---- targets ---------------------------------------------------------
    targets, diag = build_targets(cfg, t)
    print(f"targets: {diag['sleeve_counts']}  total={diag['total_weight']}  cash={diag['cash_weight']}")

    # ---- proposals -------------------------------------------------------
    p = cfg.sleeves_paper
    band, min_notional = float(p["rebalance_band"]), float(p["min_order_notional"])
    proposals = []

    # 4a. software stops — always first, always sells
    stop_rows = {r[0]: r[1] for r in conn.execute("SELECT symbol, stop_price FROM stops")}
    for sym, pos in positions.items():
        stop = stop_rows.get(sym)
        if not stop:
            continue
        if pos.is_short and pos.current_price >= stop:
            proposals.append({"symbol": sym, "side": "cover", "sleeve": "stop",
                              "notional": abs(pos.market_value),
                              "limit_price": round(pos.current_price * 1.003, 2),
                              "rationale": f"short stop: {pos.current_price} >= {stop}"})
        elif not pos.is_short and pos.current_price <= stop:
            proposals.append({"symbol": sym, "side": "sell", "sleeve": "stop",
                              "notional": pos.market_value,
                              "limit_price": round(pos.current_price * 0.997, 2),
                              "rationale": f"software stop: {pos.current_price} <= {stop}"})

    # 4b. rebalance drift
    all_syms = sorted(set(targets) | set(positions))
    for sym in all_syms:
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
        buyish = side in ("buy", "cover")
        limit = round(px * (1 + cfg.execution.max_limit_slippage_pct), 2) if buyish \
            else round(px * (1 - cfg.execution.max_limit_slippage_pct), 2)
        proposals.append({"symbol": sym, "side": side,
                          "sleeve": diag["origin"].get(sym, "rebalance"),
                          "notional": abs(diff), "limit_price": limit,
                          "rationale": f"target ${tgt_notional:,.0f} vs held ${cur_notional:,.0f}"})

    print(f"proposals: {len(proposals)}")

    # ---- gate ------------------------------------------------------------
    symbol_data = {}
    if proposals:
        need = sorted({pr["symbol"] for pr in proposals})
        bars = t.get_bars(need, today - dt.timedelta(days=90), today)
        for sym in need:
            df = bars.get(sym)
            px = t.latest_price(sym) or (float(df["close"].iloc[-1]) if df is not None and len(df) else 0.0)
            a = float(atr(df, 14).iloc[-1]) if df is not None and len(df) >= 15 else px * 0.02
            adv = float((df["close"] * df["volume"]).tail(20).mean()) if df is not None and len(df) >= 20 else 0.0
            shortable = False
            if any(pr["symbol"] == sym and pr["side"] == "short" for pr in proposals):
                try:
                    asset = t.get_asset(sym)
                    shortable = bool(asset.get("shortable") and asset.get("easy_to_borrow"))
                except Exception:
                    shortable = False
            symbol_data[sym] = SymbolData(price=px, atr14=a, avg_dollar_volume_20d=adv,
                                          listed_days=10_000,
                                          is_leveraged=sym in cfg.leveraged_symbols,
                                          shortable=shortable)

    risk_state = RiskState(
        peak_equity=st["peak_equity"], day_start_equity=st["day_start_equity"],
        month_start_equity=st["month_start_equity"],
        recent_losses={k: dt.date.fromisoformat(v) for k, v in st["recent_losses"].items()},
        halted=st["halted"],
    )
    ctx = MarketContext(now=now_et, is_trading_day=bool(clock.get("is_open")) or args.force,
                        symbols=symbol_data)
    lev = float(cfg.sleeves_paper.get("gross_leverage", 1.0))
    buying_power = None
    if lev > 1.0:
        long_exposure = sum(p2.market_value for p2 in positions.values() if not p2.is_short)
        buying_power = max(lev * equity - long_exposure, 0.0)
    result = evaluate(proposals, AccountState(equity=equity, cash=cash, positions=positions,
                                              buying_power=buying_power),
                      risk_state, ctx, cfg)

    ts = now_et.isoformat()
    for rej in result.rejected:
        conn.execute("INSERT INTO rejections VALUES (?,?,?)", (ts, rej.symbol, rej.reason))
        print(f"  REJECT {rej.symbol}: {rej.reason}")
    if result.halt_reason:
        st["halted"] = True
        print(f"  HALT: {result.halt_reason}")
    if result.flatten_all and not args.dry_run:
        print("  FLATTEN: selling everything per kill switch")
        for sym, pos in positions.items():
            px = t.latest_price(sym) or pos.current_price
            try:
                if pos.is_short:
                    o = t.submit_limit(sym, "buy", abs(pos.qty), round(px * 1.003, 2))
                else:
                    o = t.submit_limit(sym, "sell", pos.qty, round(px * 0.997, 2))
                conn.execute("INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                             (ts, sym, "sell", "killswitch", pos.qty, pos.market_value,
                              px, 0.0, result.halt_reason or "flatten", o.get("id"), o.get("status")))
            except Exception as exc:
                print(f"    flatten {sym} failed: {exc}")

    # ---- execute ---------------------------------------------------------
    submitted = 0
    for order in result.approved:
        tag = " ".join(order.adjustments) or "clean"
        print(f"  APPROVED {order.side:4} {order.symbol:6} ${order.notional:>9,.2f} "
              f"@{order.limit_price} stop={order.stop_price} [{tag}]")
        if args.dry_run:
            continue
        try:
            # Alpaca has no "short"/"cover" sides: sell-when-flat opens a
            # short, buy-when-short covers.
            alpaca_side = {"short": "sell", "cover": "buy"}.get(order.side, order.side)
            order_id = f"bot-{today:%Y%m%d}-{order.symbol}-{order.side}"
            if order.side in ("buy", "short"):
                # OTO: Alpaca activates the protective stop only after the
                # parent entry fills. The SQLite stop remains a monitoring
                # fallback, not the primary protection.
                o = t.submit_protected_limit(
                    order.symbol, alpaca_side, order.qty, order.limit_price,
                    order.stop_price, client_order_id=order_id,
                )
            else:
                o = t.submit_limit(
                    order.symbol, alpaca_side, order.qty, order.limit_price,
                    client_order_id=order_id,
                )
            conn.execute("INSERT INTO orders VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                         (ts, order.symbol, order.side, order.sleeve, order.qty,
                          order.notional, order.limit_price, order.stop_price,
                          tag, o.get("id"), o.get("status")))
            if order.side in ("buy", "short"):
                conn.execute(
                    "INSERT OR REPLACE INTO stops VALUES (?,?,?,?,?)",
                    (order.symbol, order.stop_price, order.limit_price,
                     today.isoformat(), order.sleeve))
            else:
                conn.execute("DELETE FROM stops WHERE symbol=?", (order.symbol,))
            submitted += 1
        except Exception as exc:
            print(f"    submit {order.symbol} FAILED: {exc}")

    # record realised losses for the revenge-trade block
    for order in result.approved:
        if order.side in ("sell", "cover") and order.symbol in positions:
            pos = positions[order.symbol]
            if pos.unrealized_pct < 0:
                st["recent_losses"][order.symbol] = today.isoformat()

    # ---- journal + report ------------------------------------------------
    conn.execute("INSERT INTO snapshots VALUES (?,?,?,?,?)",
                 (ts, equity, cash,
                  json.dumps({s: {"qty": p.qty, "px": p.current_price} for s, p in positions.items()}),
                  json.dumps(diag)))
    conn.commit()
    save_risk_state(st)

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    lines = [
        f"# Paper run {ts}",
        f"- equity ${equity:,.2f} | cash ${cash:,.2f} | positions {len(positions)}",
        f"- sleeves {diag['sleeve_counts']} | invested {diag['total_weight']:.0%} | cash target {diag['cash_weight']:.0%}",
        f"- proposals {len(proposals)} | approved {len(result.approved)} | rejected {len(result.rejected)} | submitted {submitted}",
    ]
    if result.halt_reason:
        lines.append(f"- **HALT: {result.halt_reason}**")
    for n in result.notes:
        lines.append(f"- note: {n}")
    (REPORT_DIR / f"{today}.md").write_text("\n".join(lines) + "\n")
    print(f"done: {submitted} orders submitted")


if __name__ == "__main__":
    main()
