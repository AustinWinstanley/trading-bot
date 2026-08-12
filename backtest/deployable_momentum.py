"""Deployable $10k MOM_LS simulator shared by execution-fidelity studies.

Unlike the idealized production headline backtest, this tracks quantities and
trades through the live drift band: fractional longs, whole-share shorts,
weekly 12-1 membership, and a 20%/$25 target-restoration threshold.  It is not
an alternative signal; it is the bridge between research weights and orders a
paper account can actually place.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from backtest.short_capacity_study import CapacityResult, STARTING_EQUITY


@dataclass
class DeployableDiagnostics:
    rejected_restorations: int = 0
    rejected_restoration_notional: float = 0.0
    trades: int = 0
    traded_notional: float = 0.0


def build_deployable_stream(
    close: pd.DataFrame,
    volume: pd.DataFrame,
    *,
    account_equity: pd.Series | float = STARTING_EQUITY,
    account_multiplier: float = 0.30,
    allow_target_restoration_at_loss: bool = False,
    lookback: int = 252,
    skip: int = 21,
    long_n: int = 20,
    short_n: int = 20,
    rebalance: int = 5,
    min_price: float = 5.0,
    min_dollar_volume: float = 5e6,
    rebalance_band: float = 0.20,
    min_order_notional: float = 25.0,
    cost_bps: float = 15.0,
) -> tuple[CapacityResult, DeployableDiagnostics]:
    """Simulate the live MOM_LS quantity and drift-restoration path.

    Returned weights are normalized to the sleeve's 100%-gross research
    stream. Multiplying them by ``account_multiplier`` produces account-level
    exposure, matching ``short_capacity_study.profile_returns``.
    """
    dollar_volume = (close * volume).rolling(20, min_periods=10).mean()
    momentum = close.shift(skip) / close.shift(lookback) - 1.0
    eligible = (
        close.shift(skip).gt(min_price)
        & dollar_volume.shift(skip).gt(min_dollar_volume)
        & momentum.notna()
        & close.notna()
    )
    daily_returns = close.pct_change(fill_method=None)
    rebalance_days = set(close.index[lookback + skip :: rebalance])
    equity = (
        pd.Series(float(account_equity), index=close.index)
        if np.isscalar(account_equity)
        else account_equity.reindex(close.index).ffill().bfill()
    )

    shares: dict[str, float] = {}
    entry: dict[str, float] = {}
    longs: list[str] = []
    shorts: list[str] = []
    weight_rows: list[pd.Series] = []
    turnover = pd.Series(0.0, index=close.index)
    logs: list[dict] = []
    diag = DeployableDiagnostics()

    for date in close.index:
        px = close.loc[date]
        if date in rebalance_days:
            ranked = momentum.loc[date].where(eligible.loc[date]).dropna()
            ranked = ranked.sort_values(ascending=False)
            if len(ranked) >= long_n + short_n:
                longs = list(ranked.head(long_n).index)
                shorts = list(ranked.index[::-1][:short_n])

        if not longs or not shorts:
            weight_rows.append(pd.Series(0.0, index=close.columns, name=date))
            continue

        eq = float(equity.loc[date])
        long_dollars = eq * account_multiplier * 0.5 / long_n
        short_dollars = eq * account_multiplier * 0.5 / short_n
        desired_qty: dict[str, float] = {
            symbol: long_dollars / float(px[symbol])
            for symbol in longs if pd.notna(px[symbol]) and px[symbol] > 0
        }
        desired_qty.update({
            symbol: -float(np.floor(short_dollars / float(px[symbol])))
            for symbol in shorts if pd.notna(px[symbol]) and px[symbol] > 0
        })

        for symbol in sorted(set(shares) | set(desired_qty)):
            price = float(px.get(symbol, np.nan))
            if not np.isfinite(price) or price <= 0:
                continue
            current_qty = shares.get(symbol, 0.0)
            target_qty = desired_qty.get(symbol, 0.0)
            diff_notional = (target_qty - current_qty) * price
            target_notional = target_qty * price
            threshold = (
                max(rebalance_band * abs(target_notional), min_order_notional)
                if target_qty else min_order_notional
            )
            if abs(diff_notional) < threshold:
                continue

            increasing = (
                current_qty > 0 and target_qty > current_qty
            ) or (
                current_qty < 0 and target_qty < current_qty
            )
            losing = (
                current_qty > 0 and price < entry.get(symbol, price)
            ) or (
                current_qty < 0 and price > entry.get(symbol, price)
            )
            if increasing and losing and not allow_target_restoration_at_loss:
                diag.rejected_restorations += 1
                diag.rejected_restoration_notional += abs(diff_notional)
                continue

            old_abs = abs(current_qty)
            new_abs = abs(target_qty)
            if target_qty == 0:
                shares.pop(symbol, None)
                entry.pop(symbol, None)
            else:
                if current_qty == 0 or np.sign(current_qty) != np.sign(target_qty):
                    entry[symbol] = price
                elif new_abs > old_abs:
                    entry[symbol] = (
                        entry[symbol] * old_abs + price * (new_abs - old_abs)
                    ) / new_abs
                shares[symbol] = target_qty
            traded = abs(diff_notional)
            diag.trades += 1
            diag.traded_notional += traded
            turnover.at[date] += traded / eq / account_multiplier

        weights = pd.Series(0.0, index=close.columns, dtype="float64", name=date)
        for symbol, qty in shares.items():
            price = px.get(symbol)
            if pd.notna(price):
                weights.at[symbol] = qty * float(price) / eq / account_multiplier
        weight_rows.append(weights)
        logs.append({
            "date": date,
            "account_equity": eq,
            "realized_short_gross": float(-weights.clip(upper=0).sum()),
            "zero_share_targets": sum(desired_qty.get(s, 0) == 0 for s in shorts),
            "selected_shorts": len(shorts),
        })

    weights = pd.DataFrame(weight_rows).reindex(close.index).fillna(0.0)
    gross = (weights.shift(1) * daily_returns).sum(axis=1)
    returns = gross - turnover * cost_bps / 10_000
    short_gross = -weights.clip(upper=0).sum(axis=1).shift(1).fillna(0.0)
    return CapacityResult(
        returns=returns,
        short_gross=short_gross,
        rebalances=pd.DataFrame(logs),
        weights=weights,
    ), diag
