"""Strategy sleeves.

Every function here is **point-in-time**: it takes an `as_of` date and may only
look at bars up to and including that date. The backtest and the live run call
exactly the same code, so a lookahead bug would show up identically in both
rather than only in the backtest — which is the whole reason for sharing it.

Sleeves:
  1. momentum       — 6-month return skipping the last month, top N, trend-filtered
  2. pead           — post-earnings drift; mechanical filter here, LLM confirms guidance
  3. mean_reversion — RSI(2) oversold above the 200DMA
  4. leveraged      — TQQQ/SOXL/UPRO, only in a confirmed calm uptrend

A note on VIX: Alpaca does not carry index quotes, so the leveraged sleeve's
"VIX < 22" gate is implemented as 20-day annualised realised volatility of the
trend symbol. It is a proxy, not the same series — see `realised_vol()`.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from engine.config import Config
from engine.data import atr, avg_dollar_volume, momentum_skip, rsi, sma

TRADING_DAYS_PER_MONTH = 21
TRADING_DAYS_PER_YEAR = 252


@dataclass
class Signal:
    sleeve: str
    symbol: str
    action: str            # "enter" | "exit"
    strength: float = 0.0  # ranking score; higher is better
    reason: str = ""
    needs_llm_confirmation: bool = False
    context: dict = field(default_factory=dict)


def _slice(df: pd.DataFrame, as_of: dt.date) -> pd.DataFrame:
    """Bars up to and including `as_of`. The only lookahead guard that matters.

    Uses `searchsorted` on the (sorted) index rather than a boolean mask: the
    mask scans every row on every call, which made the backtest O(n^2) in the
    number of trading days. This is O(log n) and returns a view.
    """
    if df is None or df.empty:
        return pd.DataFrame()
    cutoff = pd.Timestamp(as_of, tz="UTC") + pd.Timedelta(hours=23, minutes=59)
    pos = df.index.searchsorted(cutoff, side="right")
    if pos == len(df):
        return df
    return df.iloc[:pos]


def _ind(df: pd.DataFrame, name: str, fn) -> pd.Series:
    """Use a precomputed indicator column when present, else compute it.

    Safe because every indicator in this module is *causal* — a rolling or
    Wilder-smoothed function of past bars only. The value at row i is identical
    whether it was computed over `df[:i+1]` or over the whole frame and then
    indexed. `test_precomputed_indicators_match_point_in_time` asserts exactly
    that, so the fast path cannot silently drift from the correct one.

    This turns the backtest from O(n^2) into O(n) without giving the backtest
    its own indicator implementation.
    """
    if name in df.columns:
        return df[name]
    return fn(df)


def precompute_indicators(bars: dict[str, pd.DataFrame], cfg: Config) -> dict[str, pd.DataFrame]:
    """Attach causal indicator columns to every frame. Backtest-only speedup."""
    mom = cfg.sleeves["momentum"]
    lookback = int(mom["lookback_months"]) * TRADING_DAYS_PER_MONTH
    skip = int(mom["skip_months"]) * TRADING_DAYS_PER_MONTH
    rsi_period = int(cfg.sleeves["mean_reversion"]["rsi_period"])

    out: dict[str, pd.DataFrame] = {}
    for sym, df in bars.items():
        if df is None or df.empty:
            continue
        d = df.copy()
        close = d["close"]
        d["sma5"] = sma(close, 5)
        d["sma50"] = sma(close, 50)
        d["sma200"] = sma(close, 200)
        d[f"rsi{rsi_period}"] = rsi(close, rsi_period)
        d["atr14"] = atr(d, 14)
        d["adv20"] = avg_dollar_volume(d, 20)
        d[f"mom_{lookback}_{skip}"] = momentum_skip(close, lookback, skip)
        out[sym] = d
    return out


def realised_vol(close: pd.Series, period: int = 20) -> float:
    """Annualised realised volatility, in VIX-like percentage points."""
    if len(close) < period + 1:
        return float("nan")
    rets = np.log(close / close.shift(1)).dropna()
    if len(rets) < period:
        return float("nan")
    return float(rets.tail(period).std() * np.sqrt(TRADING_DAYS_PER_YEAR) * 100)


# --------------------------------------------------------------------------
# Regime
# --------------------------------------------------------------------------


def market_regime(bars: dict[str, pd.DataFrame], as_of: dt.date, benchmark: str = "SPY") -> dict:
    """Risk-on only while the benchmark is above its 200-day average."""
    df = _slice(bars.get(benchmark, pd.DataFrame()), as_of)
    if len(df) < 200:
        return {"risk_on": False, "reason": f"insufficient {benchmark} history ({len(df)} bars, need 200)"}
    close = df["close"]
    ma200 = _ind(df, "sma200", lambda d: sma(d["close"], 200)).iloc[-1]
    last = float(close.iloc[-1])
    risk_on = bool(last > ma200)
    return {
        "risk_on": risk_on,
        "benchmark": benchmark,
        "close": last,
        "sma200": float(ma200),
        "reason": f"{benchmark} {last:.2f} {'above' if risk_on else 'below'} 200DMA {ma200:.2f}",
    }


def passes_universe_filters(df: pd.DataFrame, cfg: Config) -> tuple[bool, str]:
    if df.empty:
        return False, "no bars"
    price = float(df["close"].iloc[-1])
    if price < cfg.universe.min_price:
        return False, f"price {price:.2f} < {cfg.universe.min_price:.2f}"
    if len(df) < 20:
        return False, f"only {len(df)} bars, need 20 for liquidity check"
    adv = float(_ind(df, "adv20", lambda d: avg_dollar_volume(d, 20)).iloc[-1])
    if not np.isfinite(adv) or adv < cfg.universe.min_avg_dollar_volume:
        return False, f"20d avg dollar volume {adv:,.0f} < {cfg.universe.min_avg_dollar_volume:,.0f}"
    if len(df) < cfg.universe.exclude_ipo_days:
        return False, f"only {len(df)} bars of history, IPO exclusion is {cfg.universe.exclude_ipo_days}d"
    return True, "ok"


# --------------------------------------------------------------------------
# Sleeve 1 — momentum rotation
# --------------------------------------------------------------------------


def momentum_signals(
    bars: dict[str, pd.DataFrame], as_of: dt.date, cfg: Config, held: set[str] | None = None
) -> list[Signal]:
    sleeve = cfg.sleeves["momentum"]
    held = held or set()
    regime = market_regime(bars, as_of)

    if not regime["risk_on"]:
        exits = [
            Signal("momentum", s, "exit", reason=f"risk-off: {regime['reason']}")
            for s in held
        ]
        risk_off = sleeve.get("risk_off_symbol")
        if risk_off:
            exits.append(
                Signal("momentum", risk_off, "enter", strength=1.0,
                       reason=f"risk-off rotation into {risk_off}: {regime['reason']}")
            )
        return exits

    lookback = int(sleeve["lookback_months"]) * TRADING_DAYS_PER_MONTH
    skip = int(sleeve["skip_months"]) * TRADING_DAYS_PER_MONTH

    scored: list[tuple[str, float]] = []
    for symbol in sleeve["universe"]:
        df = _slice(bars.get(symbol, pd.DataFrame()), as_of)
        ok, _ = passes_universe_filters(df, cfg)
        if not ok or len(df) < lookback + 1:
            continue
        mom = _ind(df, f"mom_{lookback}_{skip}",
                   lambda d: momentum_skip(d["close"], lookback, skip)).iloc[-1]
        if pd.notna(mom):
            scored.append((symbol, float(mom)))

    scored.sort(key=lambda x: x[1], reverse=True)
    top = [s for s, m in scored[: int(sleeve["hold_top_n"])] if m > 0]

    signals: list[Signal] = []
    for symbol in held - set(top):
        signals.append(Signal("momentum", symbol, "exit", reason="dropped out of the top ranks"))
    for rank, symbol in enumerate(top, start=1):
        if symbol in held:
            continue
        score = dict(scored)[symbol]
        signals.append(
            Signal("momentum", symbol, "enter", strength=score,
                   reason=f"rank {rank}/{len(top)}, {sleeve['lookback_months']}-{sleeve['skip_months']}mo return {score:.2%}",
                   context={"rank": rank, "momentum": score})
        )
    return signals


# --------------------------------------------------------------------------
# Sleeve 2 — post-earnings announcement drift
# --------------------------------------------------------------------------


def pead_candidates(
    bars: dict[str, pd.DataFrame], as_of: dt.date, cfg: Config, earnings_symbols: list[str]
) -> list[Signal]:
    """Mechanical half of PEAD. The LLM does the other half.

    This finds gap-ups on volume that close strong. It deliberately does NOT
    decide whether the beat was real — that requires reading the release to
    separate a raised forward guide from a one-off tax or buyback beat, which
    is the analyst run's job. Every signal returned here carries
    `needs_llm_confirmation=True` and is not tradeable until confirmed.
    """
    sleeve = cfg.sleeves["pead"]
    out: list[Signal] = []

    for symbol in earnings_symbols:
        df = _slice(bars.get(symbol, pd.DataFrame()), as_of)
        ok, why = passes_universe_filters(df, cfg)
        if not ok or len(df) < 21:
            continue

        today, prev = df.iloc[-1], df.iloc[-2]
        gap = (today["open"] - prev["close"]) / prev["close"]
        if gap < float(sleeve["min_gap_pct"]):
            continue

        avg_vol = float(df["volume"].iloc[-21:-1].mean())
        vol_mult = float(today["volume"]) / avg_vol if avg_vol > 0 else 0.0
        if vol_mult < float(sleeve["min_volume_multiple"]):
            continue

        day_range = today["high"] - today["low"]
        close_pos = (today["close"] - today["low"]) / day_range if day_range > 0 else 0.0
        if close_pos < float(sleeve["min_close_range_pct"]):
            continue

        out.append(
            Signal(
                "pead", symbol, "enter",
                strength=float(gap * vol_mult),
                reason=(
                    f"gap +{gap:.2%} on {vol_mult:.1f}x volume, closed at "
                    f"{close_pos:.0%} of the day's range"
                ),
                needs_llm_confirmation=True,
                context={
                    "gap_pct": float(gap),
                    "volume_multiple": vol_mult,
                    "close_range_position": float(close_pos),
                    "confirmation_required": (
                        "Read the earnings release and call transcript. Enter ONLY if forward "
                        "guidance was RAISED. Reject if the beat came from buybacks, a tax "
                        "item, or another one-off. Reject on any pending merger, fraud or "
                        "short-seller allegation, or going-concern language."
                    ),
                },
            )
        )

    out.sort(key=lambda s: s.strength, reverse=True)
    return out[: int(sleeve["max_positions"])]


# --------------------------------------------------------------------------
# Sleeve 3 — short-term mean reversion
# --------------------------------------------------------------------------


def mean_reversion_signals(
    bars: dict[str, pd.DataFrame], as_of: dt.date, cfg: Config,
    universe: list[str], held: dict[str, int] | None = None,
) -> list[Signal]:
    """RSI(2) oversold, but only in names already in an uptrend, in a risk-on market.

    `held` maps symbol -> days held, so the sleeve can enforce its own time stop.
    """
    sleeve = cfg.sleeves["mean_reversion"]
    held = held or {}
    signals: list[Signal] = []

    regime = market_regime(bars, as_of)
    period = int(sleeve["rsi_period"])

    # Exits first — these must fire even when the market is risk-off.
    for symbol, days_held in held.items():
        df = _slice(bars.get(symbol, pd.DataFrame()), as_of)
        if df.empty:
            continue
        r = _ind(df, f"rsi{period}", lambda d: rsi(d["close"], period)).iloc[-1]
        ma5 = _ind(df, "sma5", lambda d: sma(d["close"], 5)).iloc[-1]
        close = float(df["close"].iloc[-1])
        if pd.notna(r) and float(r) > float(sleeve["rsi_exit"]):
            signals.append(Signal("mean_reversion", symbol, "exit", reason=f"RSI({period}) {float(r):.1f} > {sleeve['rsi_exit']}"))
        elif pd.notna(ma5) and close > float(ma5):
            signals.append(Signal("mean_reversion", symbol, "exit", reason=f"close {close:.2f} > 5DMA {float(ma5):.2f}"))
        elif days_held >= int(sleeve["max_hold_days"]):
            signals.append(Signal("mean_reversion", symbol, "exit", reason=f"time stop: held {days_held}d"))

    if not regime["risk_on"]:
        return signals

    for symbol in universe:
        if symbol in held:
            continue
        df = _slice(bars.get(symbol, pd.DataFrame()), as_of)
        ok, _ = passes_universe_filters(df, cfg)
        if not ok or len(df) < 200:
            continue
        close = df["close"]
        r = _ind(df, f"rsi{period}", lambda d: rsi(d["close"], period)).iloc[-1]
        ma200 = _ind(df, "sma200", lambda d: sma(d["close"], 200)).iloc[-1]
        last = float(close.iloc[-1])
        if pd.isna(r) or pd.isna(ma200):
            continue
        if float(r) < float(sleeve["rsi_entry"]) and last > float(ma200):
            signals.append(
                Signal(
                    "mean_reversion", symbol, "enter",
                    strength=float(sleeve["rsi_entry"]) - float(r),
                    reason=f"RSI({period}) {float(r):.1f} < {sleeve['rsi_entry']} with price above 200DMA",
                    context={"rsi": float(r), "sma200": float(ma200)},
                )
            )

    signals.sort(key=lambda s: s.strength, reverse=True)
    return signals


# --------------------------------------------------------------------------
# Sleeve 4 — leveraged
# --------------------------------------------------------------------------


def leveraged_signals(
    bars: dict[str, pd.DataFrame], as_of: dt.date, cfg: Config, held: set[str] | None = None
) -> list[Signal]:
    """TQQQ/SOXL/UPRO, gated on a confirmed calm uptrend.

    The exit is unconditional and non-discretionary: trend symbol closes below
    its 50DMA and the whole sleeve goes to cash, no judgment involved. This is
    the sleeve most capable of destroying the account, so it has the least
    discretion attached to it.
    """
    sleeve = cfg.sleeves["leveraged"]
    held = held or set()
    trend_symbol = sleeve.get("trend_symbol", "QQQ")
    df = _slice(bars.get(trend_symbol, pd.DataFrame()), as_of)

    if len(df) < 200:
        return [Signal("leveraged", s, "exit", reason=f"insufficient {trend_symbol} history") for s in held]

    close = df["close"]
    last = float(close.iloc[-1])
    ma50 = float(_ind(df, "sma50", lambda d: sma(d["close"], 50)).iloc[-1])
    ma200 = float(_ind(df, "sma200", lambda d: sma(d["close"], 200)).iloc[-1])
    vol = realised_vol(close, 20)
    mom10 = float(close.iloc[-1] / close.iloc[-11] - 1.0) if len(close) > 11 else float("nan")

    # Unconditional exit.
    if last < ma50:
        return [
            Signal("leveraged", s, "exit",
                   reason=f"UNCONDITIONAL: {trend_symbol} {last:.2f} closed below 50DMA {ma50:.2f}")
            for s in held
        ]

    gates = {
        "trend": last > ma50 > ma200,
        "calm": np.isfinite(vol) and vol < float(sleeve["max_vix"]),
        "momentum": np.isfinite(mom10) and mom10 > 0,
    }
    detail = (
        f"{trend_symbol} {last:.2f} / 50DMA {ma50:.2f} / 200DMA {ma200:.2f}; "
        f"20d realised vol {vol:.1f} (limit {sleeve['max_vix']}); 10d momentum {mom10:.2%}"
    )

    if not all(gates.values()):
        failed = [k for k, v in gates.items() if not v]
        return [
            Signal("leveraged", s, "exit", reason=f"gate(s) failed {failed}: {detail}")
            for s in held
        ]

    signals: list[Signal] = []
    for symbol in sleeve["symbols"]:
        if symbol in held:
            continue
        sdf = _slice(bars.get(symbol, pd.DataFrame()), as_of)
        ok, why = passes_universe_filters(sdf, cfg)
        if not ok:
            continue
        signals.append(
            Signal("leveraged", symbol, "enter", strength=mom10,
                   reason=f"all gates passed: {detail}",
                   context={"gates": gates, "realised_vol": vol, "momentum_10d": mom10})
        )
    return signals


# --------------------------------------------------------------------------
# Aggregate
# --------------------------------------------------------------------------


def build_signals(
    bars: dict[str, pd.DataFrame],
    as_of: dt.date,
    cfg: Config,
    *,
    held_by_sleeve: dict[str, set[str]] | None = None,
    mean_reversion_universe: list[str] | None = None,
    mean_reversion_held: dict[str, int] | None = None,
    earnings_symbols: list[str] | None = None,
) -> dict:
    """Everything the analyst run needs, as plain JSON-serialisable data."""
    held_by_sleeve = held_by_sleeve or {}
    regime = market_regime(bars, as_of)

    sleeves = {
        "momentum": momentum_signals(bars, as_of, cfg, held_by_sleeve.get("momentum", set())),
        "mean_reversion": mean_reversion_signals(
            bars, as_of, cfg,
            mean_reversion_universe or cfg.sleeves["momentum"]["universe"],
            mean_reversion_held or {},
        ),
        "leveraged": leveraged_signals(bars, as_of, cfg, held_by_sleeve.get("leveraged", set())),
        "pead": pead_candidates(bars, as_of, cfg, earnings_symbols or []),
    }

    return {
        "as_of": as_of.isoformat(),
        "regime": regime,
        "sleeves": {
            name: [
                {
                    "sleeve": s.sleeve, "symbol": s.symbol, "action": s.action,
                    "strength": s.strength, "reason": s.reason,
                    "needs_llm_confirmation": s.needs_llm_confirmation,
                    "context": s.context,
                }
                for s in sigs
            ]
            for name, sigs in sleeves.items()
        },
    }
