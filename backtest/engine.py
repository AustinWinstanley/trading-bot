"""Backtest harness.

Deliberate design choices, because each one is a way backtests lie:

* **Signals on close D, fill at open D+1.** Never fill at the close that
  generated the signal — that is the most common lookahead bug in retail
  backtests and it manufactures returns that do not exist.
* **Stops are checked against the day's low, and gap-downs fill at the open.**
  Assuming a stop always fills at exactly the stop price flatters every
  drawdown; real gaps fill below it.
* **The live risk gate runs here too.** `engine/risk.py` is not re-implemented
  for the backtest — the same module decides sizing in both, so the backtest
  measures the system that will actually trade.
* **Slippage and spread are charged on every fill**, configurable in bps.

PEAD is not backtested. It depends on an LLM reading each earnings release to
confirm guidance was raised, which cannot be replayed historically, and
backtesting the mechanical half alone would measure a strategy that is not the
one being deployed. It gets forward-tested on paper instead.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from engine.config import Config
from engine.data import atr
from engine.risk import (
    AccountState,
    MarketContext,
    Position,
    RiskState,
    SymbolData,
    evaluate,
    stop_distance_pct,
)
from engine.signals import (
    build_signals,
    precompute_indicators,
    leveraged_signals,
    market_regime,
    mean_reversion_signals,
    momentum_signals,
)

TRADING_DAYS = 252


@dataclass
class BacktestConfig:
    start: dt.date
    end: dt.date
    initial_equity: float = 4600.0
    slippage_bps: float = 5.0      # 0.05% each way — realistic for liquid ETFs
    spread_bps: float = 3.0        # half-spread paid on entry and exit
    sleeves: tuple[str, ...] = ("momentum", "mean_reversion", "leveraged")
    max_concurrent_mr: int = 3     # concurrent mean-reversion positions


@dataclass
class Trade:
    symbol: str
    sleeve: str
    entry_date: dt.date
    entry_price: float
    qty: float
    exit_date: dt.date | None = None
    exit_price: float | None = None
    exit_reason: str = ""

    @property
    def pnl(self) -> float:
        if self.exit_price is None:
            return 0.0
        return (self.exit_price - self.entry_price) * self.qty

    @property
    def return_pct(self) -> float:
        if self.exit_price is None or self.entry_price == 0:
            return 0.0
        return (self.exit_price - self.entry_price) / self.entry_price

    @property
    def days_held(self) -> int:
        if self.exit_date is None:
            return 0
        return (self.exit_date - self.entry_date).days


@dataclass
class OpenPosition:
    symbol: str
    sleeve: str
    qty: float
    entry_price: float
    entry_date: dt.date
    stop_price: float
    days_held: int = 0


@dataclass
class BacktestResult:
    equity_curve: pd.Series
    trades: list[Trade]
    daily_returns: pd.Series
    gate_rejections: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    # -- metrics ---------------------------------------------------------

    def metrics(self) -> dict:
        eq = self.equity_curve.dropna()
        if len(eq) < 2:
            return {"error": "insufficient equity curve"}
        rets = self.daily_returns.dropna()
        years = max((eq.index[-1] - eq.index[0]).days / 365.25, 1e-9)
        total = eq.iloc[-1] / eq.iloc[0] - 1
        cagr = (eq.iloc[-1] / eq.iloc[0]) ** (1 / years) - 1
        vol = rets.std() * np.sqrt(TRADING_DAYS) if len(rets) > 1 else 0.0
        sharpe = (rets.mean() * TRADING_DAYS) / vol if vol > 0 else 0.0
        downside = rets[rets < 0].std() * np.sqrt(TRADING_DAYS) if (rets < 0).any() else 0.0
        sortino = (rets.mean() * TRADING_DAYS) / downside if downside > 0 else 0.0
        running_max = eq.cummax()
        dd = (eq - running_max) / running_max
        closed = [t for t in self.trades if t.exit_price is not None]
        wins = [t for t in closed if t.pnl > 0]
        losses = [t for t in closed if t.pnl <= 0]
        gross_win = sum(t.pnl for t in wins)
        gross_loss = abs(sum(t.pnl for t in losses))
        return {
            "start": eq.index[0].date().isoformat(),
            "end": eq.index[-1].date().isoformat(),
            "years": round(years, 2),
            "initial_equity": round(float(eq.iloc[0]), 2),
            "final_equity": round(float(eq.iloc[-1]), 2),
            "total_return": round(float(total), 4),
            "cagr": round(float(cagr), 4),
            "annual_vol": round(float(vol), 4),
            "sharpe": round(float(sharpe), 3),
            "sortino": round(float(sortino), 3),
            "max_drawdown": round(float(dd.min()), 4),
            "max_dd_date": dd.idxmin().date().isoformat() if len(dd) else None,
            "trades": len(closed),
            "win_rate": round(len(wins) / len(closed), 4) if closed else 0.0,
            "avg_win": round(float(np.mean([t.return_pct for t in wins])), 4) if wins else 0.0,
            "avg_loss": round(float(np.mean([t.return_pct for t in losses])), 4) if losses else 0.0,
            "profit_factor": round(gross_win / gross_loss, 3) if gross_loss > 0 else float("inf"),
            "avg_days_held": round(float(np.mean([t.days_held for t in closed])), 1) if closed else 0.0,
        }

    def monthly_returns(self) -> pd.Series:
        return self.equity_curve.resample("ME").last().pct_change().dropna()

    def sleeve_attribution(self) -> pd.DataFrame:
        rows = []
        for sleeve in sorted({t.sleeve for t in self.trades}):
            ts = [t for t in self.trades if t.sleeve == sleeve and t.exit_price is not None]
            if not ts:
                continue
            wins = [t for t in ts if t.pnl > 0]
            rows.append(
                {
                    "sleeve": sleeve,
                    "trades": len(ts),
                    "total_pnl": round(sum(t.pnl for t in ts), 2),
                    "win_rate": round(len(wins) / len(ts), 3),
                    "avg_return": round(float(np.mean([t.return_pct for t in ts])), 4),
                    "avg_days": round(float(np.mean([t.days_held for t in ts])), 1),
                }
            )
        return pd.DataFrame(rows)

    def window_performance(self, windows: dict[str, tuple[str, str]]) -> pd.DataFrame:
        """Return and drawdown inside named stress windows."""
        rows = []
        for name, (start, end) in windows.items():
            seg = self.equity_curve.loc[start:end]
            if len(seg) < 2:
                rows.append({"window": name, "status": "no data"})
                continue
            run_max = seg.cummax()
            dd = ((seg - run_max) / run_max).min()
            rows.append(
                {
                    "window": name,
                    "status": "ok",
                    "return": round(float(seg.iloc[-1] / seg.iloc[0] - 1), 4),
                    "max_dd": round(float(dd), 4),
                }
            )
        return pd.DataFrame(rows)


# --------------------------------------------------------------------------
# Simulation
# --------------------------------------------------------------------------


def _fill_price(reference: float, side: str, bt: BacktestConfig) -> float:
    """Charge slippage and half-spread against us on every fill."""
    cost = (bt.slippage_bps + bt.spread_bps) / 10_000.0
    return reference * (1 + cost) if side == "buy" else reference * (1 - cost)


def run_backtest(
    bars: dict[str, pd.DataFrame], cfg: Config, bt: BacktestConfig
) -> BacktestResult:
    # Precompute causal indicators once instead of recomputing them over the
    # full history on every simulated day. Proven equivalent to the
    # point-in-time path by test_precomputed_indicators_match_point_in_time.
    bars = precompute_indicators(bars, cfg)

    benchmark = bars.get("SPY")
    if benchmark is None or benchmark.empty:
        raise ValueError("SPY bars are required as the regime benchmark")

    calendar = [
        d for d in benchmark.index
        if bt.start <= d.date() <= bt.end
    ]
    if len(calendar) < 210:
        raise ValueError(
            f"only {len(calendar)} trading days in range; need >200 for the 200DMA warm-up"
        )

    cash = bt.initial_equity
    positions: dict[str, OpenPosition] = {}
    trades: list[Trade] = []
    # symbol -> its currently-open Trade. Scanning `trades` for the open one
    # on every exit and every daily invariant check made the whole backtest
    # O(n^2) in trade count; this keeps both O(1).
    open_trades: dict[str, Trade] = {}
    equity_dates, equity_values = [], []
    rejections: list[dict] = []
    notes: list[str] = []

    peak_equity = bt.initial_equity
    month_start_equity = bt.initial_equity
    day_start_equity = bt.initial_equity
    current_month = calendar[0].month
    recent_losses: dict[str, dt.date] = {}
    pending: list[dict] = []          # orders to fill at next open
    halted = False

    # One wide price matrix instead of a per-symbol .loc lookup on every
    # position on every day. Marking the book was O(positions) label lookups
    # twice a day; this makes it a single row fetch.
    close_mx = pd.DataFrame({s: d["close"] for s, d in bars.items()})

    def mark_to_market(ts) -> float:
        if not positions:
            return cash
        try:
            row = close_mx.loc[ts]
        except KeyError:
            return cash + sum(p.qty * p.entry_price for p in positions.values())
        total = cash
        for pos in positions.values():
            px = row.get(pos.symbol)
            total += pos.qty * (float(px) if pd.notna(px) else pos.entry_price)
        return total

    for i, ts in enumerate(calendar):
        today = ts.date()

        # ---- month / day boundaries -----------------------------------
        if ts.month != current_month:
            current_month = ts.month
            month_start_equity = mark_to_market(ts)
            if halted:
                # The -18% halt is measured against the all-time peak, so
                # clearing the flag alone re-halts on the very next bar: the
                # bot is in cash, equity cannot recover, and it is bricked
                # forever. A real restart is a human deciding to continue, and
                # that decision necessarily rebases the high-water mark.
                # Without this the 2011 drawdown froze the account for 15 years.
                peak_equity = month_start_equity
                notes.append(f"{today}: restart after halt; peak rebased to {peak_equity:,.2f}")
            halted = False
        day_start_equity = mark_to_market(ts)

        # ---- 1. fill yesterday's approved orders at today's open -------
        for order in pending:
            df = bars.get(order["symbol"])
            if df is None or ts not in df.index:
                continue
            open_px = float(df.loc[ts, "open"])
            if order["side"] == "buy":
                px = _fill_price(open_px, "buy", bt)
                qty = order["notional"] / px
                if qty * px > cash:
                    qty = cash / px
                if qty <= 0:
                    continue
                cash -= qty * px
                stop_pct = stop_distance_pct(cfg, px, order["atr"])
                positions[order["symbol"]] = OpenPosition(
                    symbol=order["symbol"], sleeve=order["sleeve"], qty=qty,
                    entry_price=px, entry_date=today, stop_price=px * (1 - stop_pct),
                )
                tr = Trade(order["symbol"], order["sleeve"], today, px, qty)
                trades.append(tr)
                open_trades[order["symbol"]] = tr
            else:
                pos = positions.pop(order["symbol"], None)
                if pos is None:
                    continue
                px = _fill_price(open_px, "sell", bt)
                cash += pos.qty * px
                t = open_trades.pop(pos.symbol, None)
                if t is not None:
                    t.exit_date, t.exit_price = today, px
                    t.exit_reason = order.get("reason", "signal exit")
                    if t.pnl <= 0:
                        recent_losses[pos.symbol] = today
        pending = []

        # ---- 2. intraday stops, checked against the low ----------------
        for symbol, pos in list(positions.items()):
            df = bars.get(symbol)
            if df is None or ts not in df.index:
                continue
            row = df.loc[ts]
            if float(row["low"]) <= pos.stop_price:
                # A gap below the stop fills at the open, not at the stop.
                raw = min(float(row["open"]), pos.stop_price)
                px = _fill_price(raw, "sell", bt)
                cash += pos.qty * px
                del positions[symbol]
                t = open_trades.pop(symbol, None)
                if t is not None:
                    t.exit_date, t.exit_price = today, px
                    t.exit_reason = "stop loss"
                    recent_losses[symbol] = today

        for pos in positions.values():
            pos.days_held += 1

        equity = mark_to_market(ts)
        equity_dates.append(ts)
        equity_values.append(equity)
        peak_equity = max(peak_equity, equity)

        # Accounting invariant: every trade still marked open must correspond to
        # a live position. A mismatch means shares were destroyed somewhere —
        # exactly the bug where a second sleeve overwrote another's position.
        orphans = set(open_trades) - set(positions)
        if orphans:
            raise AssertionError(
                f"{today}: BACKTEST ACCOUNTING LEAK — open trades with no position: {orphans}"
            )

        if i < 205:      # 200DMA warm-up
            continue

        # ---- 3. generate signals on today's close ----------------------
        held_by_sleeve: dict[str, set[str]] = {}
        for pos in positions.values():
            held_by_sleeve.setdefault(pos.sleeve, set()).add(pos.symbol)
        mr_held = {p.symbol: p.days_held for p in positions.values() if p.sleeve == "mean_reversion"}

        signals = []
        if "momentum" in bt.sleeves:
            signals += momentum_signals(bars, today, cfg, held_by_sleeve.get("momentum", set()))
        if "mean_reversion" in bt.sleeves:
            signals += mean_reversion_signals(
                bars, today, cfg, cfg.sleeves["momentum"]["universe"], mr_held
            )
        if "leveraged" in bt.sleeves:
            signals += leveraged_signals(bars, today, cfg, held_by_sleeve.get("leveraged", set()))

        # ---- 4. turn signals into proposals ----------------------------
        proposals = []
        sleeve_slots = {
            "momentum": int(cfg.sleeves["momentum"]["hold_top_n"]),
            "mean_reversion": bt.max_concurrent_mr,
            "leveraged": len(cfg.sleeves["leveraged"]["symbols"]),
        }
        mr_open = sum(1 for p in positions.values() if p.sleeve == "mean_reversion")

        for sig in signals:
            df = bars.get(sig.symbol)
            if df is None or ts not in df.index:
                continue
            close_px = float(df.loc[ts, "close"])
            if sig.action == "exit":
                if sig.symbol in positions:
                    proposals.append({
                        "symbol": sig.symbol, "side": "sell", "sleeve": sig.sleeve,
                        "notional": positions[sig.symbol].qty * close_px,
                        "limit_price": close_px, "rationale": sig.reason,
                    })
                continue

            # A broker holds ONE position per symbol — there is no such thing as
            # a separate "momentum XLU" and "mean-reversion XLU". Letting two
            # sleeves open the same ticker silently destroyed the first
            # position's shares (cash spent, position record overwritten, never
            # sold, never marked to market). First sleeve to claim a symbol
            # owns it until it exits.
            if sig.symbol in positions or sig.symbol in {p["symbol"] for p in proposals if p["side"] == "buy"}:
                continue

            if sig.sleeve == "mean_reversion" and mr_open >= bt.max_concurrent_mr:
                continue
            alloc = float(cfg.sleeves[sig.sleeve]["allocation"])
            slots = max(sleeve_slots.get(sig.sleeve, 1), 1)
            notional = equity * alloc / slots
            proposals.append({
                "symbol": sig.symbol, "side": "buy", "sleeve": sig.sleeve,
                "notional": notional, "limit_price": close_px, "rationale": sig.reason,
            })
            if sig.sleeve == "mean_reversion":
                mr_open += 1

        # ---- 5. the live risk gate, unchanged --------------------------
        symbol_data = {}
        for symbol in {p["symbol"] for p in proposals}:
            df = bars.get(symbol)
            if df is None or ts not in df.index:
                continue
            row = df.loc[ts]
            px = float(row["close"])
            a = float(row.get("atr14", float("nan")))
            adv = float(row.get("adv20", float("nan")))
            symbol_data[symbol] = SymbolData(
                price=px,
                atr14=a if np.isfinite(a) else px * 0.02,
                avg_dollar_volume_20d=adv if np.isfinite(adv) else 0.0,
                listed_days=int(df.index.get_loc(ts)) + 1,
                is_leveraged=symbol in cfg.leveraged_symbols,
            )

        try:
            close_row = close_mx.loc[ts]
        except KeyError:
            close_row = None
        account = AccountState(
            equity=equity, cash=cash,
            positions={
                s: Position(
                    s, p.qty, p.entry_price,
                    float(close_row[s]) if close_row is not None and pd.notna(close_row.get(s))
                    else p.entry_price,
                )
                for s, p in positions.items() if s in bars
            },
        )
        risk_state = RiskState(
            peak_equity=peak_equity, day_start_equity=day_start_equity,
            month_start_equity=month_start_equity,
            recent_losses=dict(recent_losses), halted=halted,
        )
        ctx = MarketContext(
            now=dt.datetime.combine(today, dt.time(12, 30), tzinfo=dt.timezone.utc),
            is_trading_day=True, symbols=symbol_data,
        )

        result = evaluate(proposals, account, risk_state, ctx, cfg)
        for rej in result.rejected:
            rejections.append({"date": today.isoformat(), "symbol": rej.symbol, "reason": rej.reason})

        if result.halt_reason:
            halted = True
            notes.append(f"{today}: HALT — {result.halt_reason}")

        if result.flatten_all:
            for symbol, pos in list(positions.items()):
                px_ref = float(bars[symbol].loc[ts, "close"]) if ts in bars[symbol].index else pos.entry_price
                pending.append({
                    "symbol": symbol, "side": "sell", "sleeve": pos.sleeve,
                    "notional": pos.qty * px_ref, "atr": 0.0,
                    "reason": result.halt_reason or "kill switch flatten",
                })
            continue

        for order in result.approved:
            pending.append({
                "symbol": order.symbol, "side": order.side, "sleeve": order.sleeve,
                "notional": order.notional, "atr": symbol_data[order.symbol].atr14,
                "reason": "signal exit" if order.side == "sell" else "entry",
            })

    equity_curve = pd.Series(equity_values, index=pd.DatetimeIndex(equity_dates))
    return BacktestResult(
        equity_curve=equity_curve,
        trades=trades,
        daily_returns=equity_curve.pct_change(),
        gate_rejections=rejections,
        notes=notes,
    )
