"""Live-gate-faithful $10k simulator for ANY daily target-weight frame.

`backtest.deployable_momentum.build_deployable_stream` bridged MOM_LS research
weights to the orders a paper account can actually place, but its selection
logic and its execution accounting are one function. The replacement
programme (docs/research.md, "Live results and the 2026-09-14 stand-down")
needs the same bridge for candidates that are not momentum sleeves at all —
vol-scaled long-only momentum, a trend-filtered leveraged-index sleeve, HAA —
and needs them judged under the constraints `scripts/run_daily.py` and
`engine/risk.py` actually impose. `simulate_targets` is that bridge: give it
a daily target-weight frame (fraction of equity, negative = short) and a
close panel, and it walks the book through the live gate's rules, in the
live gate's order:

  1. drift band — `scripts/run_daily.rebalance_threshold`: a trade is
     proposed only when |target - held| >= max(min(band * |target|,
     band_equity_cap * equity), min_order_notional); a held position with
     no target is a full exit and always trades;
  2. side geometry — a sign flip closes first and opens on a LATER day,
     exactly as run_daily does ("never both in one order");
  3. no averaging down — an increase to a position that is under water is
     rejected (`risk.allow_averaging_down: false`, "never true");
  4. per-name position cap (elevated for a declared symbol set, mirroring
     `risk.elevated_position_sleeves`), applied as room on the BUY, so an
     over-cap position that drifted there is never force-sold;
  5. leveraged-ETF exposure cap across the declared symbol set;
  6. buying headroom = gross_leverage * equity - pre-trade long exposure,
     consumed by the day's buys in symbol order — same-day sells do NOT
     free it, so a full rotation on a fully-invested book lags a day, as
     it does live;
  7. whole-share floor for every short (Alpaca refuses fractional short
     legs) and for any symbol in `whole_share_symbols`: an opening order
     that floors below one share is rejected, a partial cover that floors
     below one share (or would leave less than one) is promoted to a full
     close (`engine/risk._exit_quantity`);
  8. any shrink that leaves an order under `min_order_notional` rejects it
     ("not worth a broker round-trip");
  9. costs — `cost_bps` per unit of one-way traded notional, short borrow
     on the previous day's realized short gross, margin on the previous
     day's long exposure above equity (`production_portfolio` constants).

Returns are `(PnL - costs) / prior equity` and equity compounds, so slot
sizes grow with the account the way they do live. Pass an explicit equity
`pd.Series` to size against an exogenous path instead (what
`build_deployable_stream`'s `account_equity` does).

Panel convention, shared with every study on `state/xsec`: a day on which
either the previous or the current close is missing contributes no P&L for
that symbol (`pct_change(fill_method=None)` semantics), so a data gap is a
mark discontinuity, not a return.

`deployable_compat=True` switches four rules to `build_deployable_stream`'s
conventions so the accounting can be validated against it to ~1e-8
(`tests/test_deployable_sim.py`): the whole-share floor is applied to the
TARGET quantity rather than the order, a full exit is subject to
`min_order_notional`, a sign flip trades in one step, and there is no band
cap unless one is passed. Every one of those is a place the live gate
behaves differently from the older simulator; leave the flag off for any
number that will be judged.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from backtest.production_portfolio import MARGIN_RATE, SHORT_BORROW, TD
from backtest.short_capacity_study import STARTING_EQUITY

DEFAULT_COST_BPS = 15.0
_WHOLE_SHARE_TOL = 1e-9

DIAGNOSTIC_COLUMNS = [
    "equity",
    "long_exposure",
    "short_exposure",
    "gross_exposure",
    "cash",
    "turnover",
    "trades",
    "traded_notional",
    "rejected_notional",
    "shrunk_notional",
    "rejected_whole_share",
    "rejected_averaging_down",
    "rejected_headroom",
    "trade_cost",
    "borrow_cost",
    "margin_cost",
]


@dataclass
class _Book:
    qty: dict[str, float] = field(default_factory=dict)
    entry: dict[str, float] = field(default_factory=dict)

    def open_or_add(self, symbol: str, delta_qty: float, price: float) -> None:
        current = self.qty.get(symbol, 0.0)
        new = current + delta_qty
        if current == 0.0 or np.sign(current) != np.sign(new):
            self.entry[symbol] = price
        elif abs(new) > abs(current):
            self.entry[symbol] = (
                self.entry.get(symbol, price) * abs(current)
                + price * (abs(new) - abs(current))
            ) / abs(new)
        if abs(new) < 1e-12:
            self.qty.pop(symbol, None)
            self.entry.pop(symbol, None)
        else:
            self.qty[symbol] = new

    def reduce(self, symbol: str, close_qty: float) -> None:
        current = self.qty[symbol]
        new = current - math.copysign(close_qty, current)
        if abs(new) < 1e-12 or np.sign(new) != np.sign(current):
            self.qty.pop(symbol, None)
            self.entry.pop(symbol, None)
        else:
            self.qty[symbol] = new


def _cost_bps(symbol: str, cost_bps_by_symbol) -> float:
    if isinstance(cost_bps_by_symbol, dict):
        return float(cost_bps_by_symbol.get(symbol, DEFAULT_COST_BPS))
    return float(cost_bps_by_symbol)


def _whole_qty(raw: float) -> float:
    """Floor a share count, tolerating float noise on an exact integer."""
    nearest = round(raw)
    if abs(raw - nearest) < _WHOLE_SHARE_TOL:
        return float(nearest)
    return float(math.floor(raw))


def simulate_targets(
    target_weights: pd.DataFrame,
    close: pd.DataFrame,
    *,
    equity: float | pd.Series = STARTING_EQUITY,
    gross_leverage: float = 1.0,
    whole_share_symbols: set[str] | None = None,
    rebalance_band: float = 0.20,
    band_equity_cap: float | None = 0.05,
    min_order_notional: float = 25.0,
    position_cap_pct: float = 0.15,
    elevated_cap: tuple[set[str], float] | None = None,
    leveraged_symbols: set[str] = frozenset(),
    leveraged_cap_pct: float = 0.20,
    cost_bps_by_symbol: dict[str, float] | float = DEFAULT_COST_BPS,
    borrow_rate: float = SHORT_BORROW,
    margin_rate: float = MARGIN_RATE,
    allow_averaging_down: bool = False,
    deployable_compat: bool = False,
    record_positions: bool = False,
) -> tuple[pd.Series, pd.DataFrame]:
    """Walk a daily target-weight frame through the live gate; see module doc.

    `target_weights` — dates x symbols, fraction of equity, negative = short.
    Its dates must be a subset of `close.index`; a target persists until the
    next row changes it (forward-filled), and is zero before its first row.
    `close` — the price panel the targets were built on; NaN = no bar.
    `equity` — starting equity (compounding) or an exogenous equity path.
    `whole_share_symbols` — symbols that are whole-share on the long side
    too. Shorts are always whole-share regardless.
    `elevated_cap` — `(symbols, pct)`: those symbols use `pct` instead of
    `position_cap_pct` (the SPY core's `risk.elevated_position_pct`).
    `cost_bps_by_symbol` — a flat bps figure or a per-symbol dict (symbols
    absent from the dict pay `DEFAULT_COST_BPS`).

    Returns `(returns, diagnostics)`: the daily net return series on
    `close.index` and a frame of `DIAGNOSTIC_COLUMNS` per day (exposures in
    dollars at last-known prices, `turnover` as traded notional / equity).
    With `record_positions=True` the end-of-day share counts are attached
    as `diagnostics.attrs["positions"]` (dates x symbols) for audits.
    """
    if not target_weights.index.isin(close.index).all():
        extra = target_weights.index.difference(close.index)
        raise ValueError(f"target dates not in close panel: {list(extra[:5])}")
    unknown = target_weights.columns.difference(close.columns)
    if len(unknown):
        raise ValueError(f"target symbols not in close panel: {list(unknown[:5])}")
    if gross_leverage <= 0:
        raise ValueError("gross_leverage must be positive")

    symbols = list(target_weights.columns)
    index = close.index
    prices = close[symbols].to_numpy(dtype="float64")
    targets = (
        target_weights.reindex(index).ffill().fillna(0.0).to_numpy(dtype="float64")
    )
    if isinstance(equity, pd.Series):
        equity_path = equity.reindex(index).ffill().bfill().to_numpy(dtype="float64")
        compounding = False
        current_equity = float(equity_path[0])
    else:
        equity_path = None
        compounding = True
        current_equity = float(equity)
    whole_share_symbols = set(whole_share_symbols or ())
    elevated_symbols, elevated_pct = (set(), position_cap_pct)
    if elevated_cap is not None:
        elevated_symbols, elevated_pct = set(elevated_cap[0]), float(elevated_cap[1])
    leveraged_symbols = set(leveraged_symbols)
    col = {symbol: j for j, symbol in enumerate(symbols)}

    book = _Book()
    last_px = np.full(len(symbols), np.nan)
    prev_px = np.full(len(symbols), np.nan)
    returns = np.zeros(len(index))
    rows: list[dict] = []
    position_rows: list[dict] = []

    for t in range(len(index)):
        px = prices[t]
        finite = np.isfinite(px)
        equity_prev = current_equity

        # ---- mark to market (panel convention: gaps contribute nothing) ----
        pnl = 0.0
        long_prev = short_prev = 0.0
        for symbol, qty in book.qty.items():
            j = col[symbol]
            if np.isfinite(prev_px[j]):
                notional_prev = qty * prev_px[j]
                if notional_prev > 0:
                    long_prev += notional_prev
                else:
                    short_prev -= notional_prev
                if finite[j]:
                    pnl += qty * (px[j] - prev_px[j])
        borrow_cost = short_prev * borrow_rate / TD
        margin_cost = max(long_prev - equity_prev, 0.0) * margin_rate / TD

        if compounding:
            sizing_equity = equity_prev + pnl
        else:
            sizing_equity = float(equity_path[t])

        # ---- pre-trade exposure at last-known marks (what the gate sees) ----
        last_px = np.where(finite, px, last_px)
        long_pre = 0.0
        leveraged_pre = 0.0
        for symbol, qty in book.qty.items():
            j = col[symbol]
            if qty > 0 and np.isfinite(last_px[j]):
                value = qty * last_px[j]
                long_pre += value
                if symbol in leveraged_symbols:
                    leveraged_pre += value
        headroom = gross_leverage * sizing_equity - long_pre
        leveraged_room = leveraged_cap_pct * sizing_equity - leveraged_pre

        # ---- the day's proposals, one per symbol, in symbol order ----
        trade_cost = traded = rejected = shrunk = 0.0
        trades = rej_ws = rej_avg = rej_head = 0
        active = set(np.flatnonzero(targets[t] != 0.0).tolist())
        active |= {col[s] for s in book.qty}
        for j in sorted(active):
            symbol = symbols[j]
            price = px[j]
            if not np.isfinite(price) or price <= 0:
                continue  # no bar: the position is carried untouched
            cur_qty = book.qty.get(symbol, 0.0)
            cur_notional = cur_qty * price
            weight = targets[t, j]
            tgt_notional = weight * sizing_equity
            is_whole = (
                tgt_notional < 0 or cur_qty < 0 or symbol in whole_share_symbols
            )
            if deployable_compat and is_whole and tgt_notional != 0:
                tgt_qty = math.copysign(
                    math.floor(abs(tgt_notional) / price), tgt_notional
                )
                tgt_notional = tgt_qty * price
            full_exit = tgt_notional == 0 and cur_qty != 0
            diff = tgt_notional - cur_notional

            if full_exit and not deployable_compat:
                threshold = 0.0
            elif tgt_notional == 0:
                threshold = min_order_notional
            else:
                band_notional = rebalance_band * abs(tgt_notional)
                if band_equity_cap is not None:
                    band_notional = min(band_notional, band_equity_cap * sizing_equity)
                threshold = max(band_notional, min_order_notional)
            if abs(diff) < threshold:
                continue

            flip = cur_qty != 0 and tgt_notional != 0 and np.sign(cur_qty) != np.sign(tgt_notional)
            if flip and deployable_compat:
                # build_deployable_stream reverses in one trade.
                new_qty = tgt_notional / price
                traded_here = abs(diff)
                book.qty.pop(symbol, None)
                book.entry.pop(symbol, None)
                book.open_or_add(symbol, new_qty, price)
                trade_cost += traded_here * _cost_bps(symbol, cost_bps_by_symbol) / 10_000
                traded += traded_here
                trades += 1
                continue
            if flip:
                diff = -cur_notional  # close first; the opening leg waits for a later day
            reducing = (cur_qty > 0 and diff < 0) or (cur_qty < 0 and diff > 0)
            if reducing and abs(diff) > abs(cur_notional):
                diff = -cur_notional
            order = abs(diff)
            if order < min_order_notional and not full_exit:
                continue

            if reducing:
                if full_exit or flip:
                    close_qty = abs(cur_qty)
                else:
                    raw_qty = order / price
                    if is_whole:
                        floored = _whole_qty(raw_qty)
                        if floored < 1 or abs(cur_qty) - floored < 1:
                            close_qty = abs(cur_qty)
                        else:
                            close_qty = floored
                    else:
                        close_qty = raw_qty
                    close_qty = min(close_qty, abs(cur_qty))
                traded_here = close_qty * price
                book.reduce(symbol, close_qty)
                trade_cost += traded_here * _cost_bps(symbol, cost_bps_by_symbol) / 10_000
                traded += traded_here
                trades += 1
                continue

            # ---- opening or increasing ----
            side_sign = 1.0 if diff > 0 else -1.0
            if cur_qty != 0 and not allow_averaging_down:
                entry = book.entry.get(symbol, price)
                losing = price < entry if cur_qty > 0 else price > entry
                if losing:
                    rej_avg += 1
                    rejected += order
                    continue
            cap_pct = elevated_pct if symbol in elevated_symbols else position_cap_pct
            room = cap_pct * sizing_equity - abs(cur_notional)
            if room <= 0:
                rejected += order
                continue
            if order > room:
                shrunk += order - room
                order = room
            if side_sign > 0 and symbol in leveraged_symbols:
                if leveraged_room <= 0:
                    rejected += order
                    continue
                if order > leveraged_room:
                    shrunk += order - leveraged_room
                    order = leveraged_room
            if side_sign > 0:
                if headroom <= 0:
                    rej_head += 1
                    rejected += order
                    continue
                if order > headroom:
                    shrunk += order - headroom
                    order = headroom
            if is_whole:
                qty = _whole_qty(order / price)
                if qty < 1:
                    rej_ws += 1
                    rejected += order
                    continue
                order = qty * price
            else:
                qty = order / price
            if order < min_order_notional:
                rejected += order
                continue

            book.open_or_add(symbol, side_sign * qty, price)
            if side_sign > 0:
                headroom -= order
                if symbol in leveraged_symbols:
                    leveraged_room -= order
            trade_cost += order * _cost_bps(symbol, cost_bps_by_symbol) / 10_000
            traded += order
            trades += 1

        # ---- settle the day ----
        if compounding:
            net = pnl - trade_cost - borrow_cost - margin_cost
            r = net / equity_prev
            current_equity = equity_prev + net
        else:
            r = (
                (pnl - borrow_cost - margin_cost) / equity_prev
                - trade_cost / sizing_equity
            )
            current_equity = sizing_equity  # tomorrow's "prior equity" is today's path value
        returns[t] = r

        long_post = short_post = 0.0
        for symbol, qty in book.qty.items():
            j = col[symbol]
            if np.isfinite(last_px[j]):
                value = qty * last_px[j]
                if value > 0:
                    long_post += value
                else:
                    short_post -= value
        equity_mark = current_equity
        rows.append({
            "equity": equity_mark,
            "long_exposure": long_post,
            "short_exposure": short_post,
            "gross_exposure": long_post + short_post,
            "cash": equity_mark - long_post + short_post,
            "turnover": traded / sizing_equity if sizing_equity else 0.0,
            "trades": trades,
            "traded_notional": traded,
            "rejected_notional": rejected,
            "shrunk_notional": shrunk,
            "rejected_whole_share": rej_ws,
            "rejected_averaging_down": rej_avg,
            "rejected_headroom": rej_head,
            "trade_cost": trade_cost,
            "borrow_cost": borrow_cost,
            "margin_cost": margin_cost,
        })
        if record_positions:
            position_rows.append(dict(book.qty))
        prev_px = px

    diagnostics = pd.DataFrame(rows, index=index, columns=DIAGNOSTIC_COLUMNS)
    if record_positions:
        diagnostics.attrs["positions"] = (
            pd.DataFrame(position_rows, index=index).reindex(columns=symbols).fillna(0.0)
        )
    return pd.Series(returns, index=index, name="returns"), diagnostics


def mom_ls_target_weights(
    close: pd.DataFrame,
    volume: pd.DataFrame,
    *,
    account_multiplier: float = 0.30,
    lookback: int = 252,
    skip: int = 21,
    long_n: int = 20,
    short_n: int = 20,
    rebalance: int = 5,
    rebalance_offsets: tuple[int, ...] = (0,),
    min_price: float = 5.0,
    min_dollar_volume: float = 5e6,
) -> pd.DataFrame:
    """The MOM_LS membership `build_deployable_stream` trades, as a daily
    target-weight frame (fraction of equity, +/- account_multiplier/2 split
    equally across the long and short books).

    Selection is the same 12-1 momentum rank on the same eligibility filter,
    refreshed on the same fixed-step rebalance days; when fewer than
    `long_n + short_n` names are eligible the previous membership is kept.
    Only symbols that were ever selected appear as columns. This exists so
    `simulate_targets` can be validated against the older simulator, and so
    long-only / vol-scaled variants can start from the identical membership.
    """
    dollar_volume = (close * volume).rolling(20, min_periods=10).mean()
    momentum = close.shift(skip) / close.shift(lookback) - 1.0
    eligible = (
        close.shift(skip).gt(min_price)
        & dollar_volume.shift(skip).gt(min_dollar_volume)
        & momentum.notna()
        & close.notna()
    )
    rebalance_days: set = set()
    for offset in rebalance_offsets:
        rebalance_days |= set(close.index[lookback + skip + offset :: rebalance])

    long_w = account_multiplier * 0.5 / long_n
    short_w = -account_multiplier * 0.5 / short_n
    membership: list[tuple[pd.Timestamp, list[str], list[str]]] = []
    longs: list[str] = []
    shorts: list[str] = []
    for date in sorted(rebalance_days):
        ranked = momentum.loc[date].where(eligible.loc[date]).dropna()
        ranked = ranked.sort_values(ascending=False)
        if len(ranked) < long_n + short_n:
            continue
        longs = list(ranked.head(long_n).index)
        shorts = list(ranked.index[::-1][:short_n])
        membership.append((date, longs, shorts))

    selected = sorted({s for _, lo, sh in membership for s in lo + sh})
    weights = pd.DataFrame(0.0, index=close.index, columns=selected)
    for k, (date, lo, sh) in enumerate(membership):
        start = close.index.get_loc(date)
        end = close.index.get_loc(membership[k + 1][0]) if k + 1 < len(membership) else len(close.index)
        weights.iloc[start:end, [weights.columns.get_loc(s) for s in lo]] = long_w
        weights.iloc[start:end, [weights.columns.get_loc(s) for s in sh]] = short_w
    return weights
