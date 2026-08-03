"""Pre-registered study: do the live risk controls belong on the MOM_LS sleeve?

``backtest/production_portfolio.py`` validates MOM_LS as a pure weekly 12-1
rebalance.  It models no stop-loss and no re-entry block.  Production adds
both, from ``config.yaml``:

* a per-position stop at ``min(max(stop_loss_pct, stop_atr_multiple * ATR14 /
  price), max_stop_distance_pct)`` -- 8% floor, 2x ATR, 15% cap;
* ``loss_reentry_block_days``, which bars re-entry into any name exited at a
  loss for 5 days.

Neither has ever been backtested on this sleeve.  The deployed strategy is
therefore not the strategy that earned the reported Sharpe.  This study
measures what those two controls do to the sleeve, separately and together.

The control variant is production_portfolio's construction exactly, so
"no overlay" reproduces the already-accepted result rather than introducing a
new candidate.  The question is whether the *live additions* survive contact
with the same evidence bar every other change here has had to clear.

Limitations
-----------
* The cross-sectional cache carries close and volume only, so ATR14 cannot be
  computed.  The ATR variant uses a close-to-close proxy inflated by
  ``TRUE_RANGE_INFLATION``; the flat 8% and flat 15% variants bracket it and
  are reported so no conclusion rests on the proxy alone.
* Stops are evaluated on closes.  Production checks fractional-long stops in
  software at run time (also close-like), but places real broker stops for
  whole-share shorts, which can trigger intraday.  Short stop-outs are
  therefore an optimistic bound.
* Historical borrowability is unavailable and the universe is
  survivorship-biased, as in every study on this sleeve.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from backtest.production_portfolio import (
    MARGIN_RATE,
    SHORT_BORROW,
    build_streams,
    norm_index,
    returns_summary,
)
from backtest.promotion import passes_gate_all_cells
from backtest.short_capacity_study import (
    MOM_ACCOUNT_MULTIPLIER,
    STARTING_EQUITY,
    profile_returns,
)
from backtest.xsec_data import load

TD = 252
# ATR14 measures true range; a close-to-close mean absolute move understates it.
# 1.4 is the conventional mid-point of the usual 1.3-1.6 range for liquid US
# equities. The flat-stop variants exist so the verdict does not depend on it.
TRUE_RANGE_INFLATION = 1.4

WINDOWS = {
    "early_2020_2022": slice(None, "2022-12-31"),
    "heldout_2023_plus": slice("2023-01-01", None),
}


@dataclass
class OverlayResult:
    returns: pd.Series
    short_gross: pd.Series
    weights: pd.DataFrame
    stop_exits: int = 0
    loss_exits: int = 0
    blocked_selections: int = 0
    diagnostics: dict = field(default_factory=dict)


def stop_distance(
    atr_pct: pd.Series,
    *,
    floor: float,
    multiple: float,
    cap: float,
) -> pd.Series:
    """Mirror engine/risk.py: min(max(floor, multiple * ATR/price), cap)."""
    return np.minimum(np.maximum(floor, multiple * atr_pct), cap)


def diverse_select(
    ordered: list[str],
    daily_returns: pd.DataFrame,
    date: pd.Timestamp,
    n: int,
    *,
    max_correlation: float,
    window: int,
    pool: int,
) -> list[str]:
    """Walk the ranks, skipping names too correlated with those already taken.

    Needs no sector classification -- the point of using realized correlation
    is that the dated symbol-to-industry map
    ``reports/sector_neutral_momentum_feasibility.json`` requires does not
    exist, while returns are already in hand. It is a proxy for the same
    concentration and, unlike a present-day sector label, carries no
    classification look-ahead.
    """
    candidates = ordered[:pool]
    history = daily_returns.loc[:date].tail(window)
    if len(history) < window // 2:
        return ordered[:n]
    sub = history[[c for c in candidates if c in history.columns]].dropna(axis=1, how="any")
    if sub.shape[1] < n:
        return ordered[:n]
    corr = sub.corr().abs()

    picked: list[str] = []
    for symbol in candidates:
        if symbol not in corr.columns:
            continue
        if picked and corr.loc[symbol, picked].max() > max_correlation:
            continue
        picked.append(symbol)
        if len(picked) == n:
            return picked
    # Not enough diverse names: top up from the ranking to keep sizing constant.
    for symbol in ordered:
        if symbol not in picked:
            picked.append(symbol)
        if len(picked) == n:
            break
    return picked


def build_overlay_stream(
    close: pd.DataFrame,
    volume: pd.DataFrame,
    *,
    stop_mode: str = "none",
    block_days: int = 0,
    max_correlation: float | None = None,
    corr_window: int = 60,
    corr_pool: int = 120,
    stop_floor: float = 0.08,
    stop_multiple: float = 2.0,
    stop_cap: float = 0.15,
    flat_stop: float | None = None,
    lookback: int = 252,
    skip: int = 21,
    long_n: int = 20,
    short_n: int = 20,
    rebalance: int = 5,
    min_price: float = 5.0,
    min_dollar_volume: float = 5e6,
    cost_bps: float = 15.0,
) -> OverlayResult:
    """MOM_LS with an optional stop-loss and loss re-entry block.

    ``stop_mode='none'`` with ``block_days=0`` reproduces the production
    backtest. Weights are carried daily rather than only at rebalances so a
    stop can remove a name mid-period, which is the whole point of the
    exercise -- a flat weight matrix cannot express path dependence.
    """
    if stop_mode not in {"none", "atr", "flat"}:
        raise ValueError(f"unknown stop_mode {stop_mode!r}")
    if stop_mode == "flat" and flat_stop is None:
        raise ValueError("stop_mode='flat' requires flat_stop")

    dollar_volume = (close * volume).rolling(20, min_periods=10).mean()
    momentum = close.shift(skip) / close.shift(lookback) - 1.0
    eligible = (
        close.shift(skip).gt(min_price)
        & dollar_volume.shift(skip).gt(min_dollar_volume)
        & momentum.notna()
        & close.notna()
    )
    daily_returns = close.pct_change()

    # Close-to-close proxy for ATR14/price, inflated toward true range.
    atr_pct = (
        close.pct_change().abs().rolling(14, min_periods=7).mean()
        * TRUE_RANGE_INFLATION
    )

    all_days = list(close.index[lookback + skip :])
    rebalance_days = set(close.index[lookback + skip :: rebalance])

    # symbol -> {"side": +1/-1, "weight": float, "stop": float}
    held: dict[str, dict] = {}
    blocked: dict[str, pd.Timestamp] = {}
    rows: dict[pd.Timestamp, pd.Series] = {}
    stop_exits = loss_exits = blocked_selections = 0

    for date in all_days:
        if date in rebalance_days:
            ranked = momentum.loc[date].where(eligible.loc[date]).dropna()
            ranked = ranked.sort_values(ascending=False)
            if len(ranked) >= long_n + short_n:
                if block_days > 0:
                    cutoff = date - pd.Timedelta(days=block_days)
                    barred = {s for s, when in blocked.items() if when > cutoff}
                else:
                    barred = set()

                available = [s for s in ranked.index if s not in barred]
                if max_correlation is None:
                    longs = available[:long_n]
                    shorts = available[::-1][:short_n]
                else:
                    longs = diverse_select(
                        available, daily_returns, date, long_n,
                        max_correlation=max_correlation,
                        window=corr_window, pool=corr_pool,
                    )
                    shorts = diverse_select(
                        available[::-1], daily_returns, date, short_n,
                        max_correlation=max_correlation,
                        window=corr_window, pool=corr_pool,
                    )
                blocked_selections += sum(
                    1
                    for s in list(ranked.index[:long_n]) + list(ranked.index[::-1][:short_n])
                    if s in barred
                )

                new_held: dict[str, dict] = {}
                for side, members, n in ((1, longs, long_n), (-1, shorts, short_n)):
                    for symbol in members:
                        price = float(close.at[date, symbol])
                        if not np.isfinite(price) or price <= 0:
                            continue
                        prior = held.get(symbol)
                        if prior is not None and prior["side"] == side:
                            # Incumbent keeps its original stop reference.
                            new_held[symbol] = {**prior, "weight": side * 0.5 / n}
                            continue
                        if stop_mode == "none":
                            stop = np.nan
                        else:
                            if stop_mode == "flat":
                                dist = float(flat_stop)
                            else:
                                raw = atr_pct.at[date, symbol]
                                if not np.isfinite(raw):
                                    raw = stop_floor / stop_multiple
                                dist = float(
                                    stop_distance(
                                        pd.Series([raw]),
                                        floor=stop_floor,
                                        multiple=stop_multiple,
                                        cap=stop_cap,
                                    )[0]
                                )
                            stop = price * (1 - dist) if side == 1 else price * (1 + dist)
                        new_held[symbol] = {
                            "side": side,
                            "weight": side * 0.5 / n,
                            "stop": stop,
                            "entry": price,
                        }

                # Production records a block for ANY exit at an unrealized
                # loss (scripts/run_daily.py, "revenge-trade block"), not just
                # stop-outs -- so a name rotated out of the ranks while under
                # water is barred too. Modelling only stop exits would make the
                # block look free.
                for symbol, pos in held.items():
                    if symbol in new_held:
                        continue
                    exit_px = close.at[date, symbol]
                    if not np.isfinite(exit_px):
                        continue
                    if (exit_px - pos["entry"]) * pos["side"] < 0:
                        loss_exits += 1
                        if block_days > 0:
                            blocked[symbol] = date
                held = new_held

        # Stop check on the day's close, after any rebalance.
        if stop_mode != "none" and held:
            for symbol in list(held):
                pos = held[symbol]
                stop = pos["stop"]
                if not np.isfinite(stop):
                    continue
                price = close.at[date, symbol]
                if not np.isfinite(price):
                    continue
                hit = price <= stop if pos["side"] == 1 else price >= stop
                if hit:
                    stop_exits += 1
                    entry = pos["entry"]
                    pnl = (price - entry) * pos["side"]
                    if pnl < 0:
                        loss_exits += 1
                        blocked[symbol] = date
                    del held[symbol]

        if held:
            rows[date] = pd.Series(
                {s: p["weight"] for s, p in held.items()}, dtype="float64"
            )

    if not rows:
        empty = pd.Series(0.0, index=close.index)
        return OverlayResult(
            empty, empty, pd.DataFrame(0.0, index=close.index, columns=close.columns)
        )

    weights = (
        pd.DataFrame.from_dict(rows, orient="index")
        .reindex(columns=close.columns)
        .reindex(close.index)
        .fillna(0.0)
    )
    gross = (weights.shift(1) * daily_returns).sum(axis=1)
    turnover = weights.diff().abs().sum(axis=1)
    returns = gross - turnover * cost_bps / 10_000.0
    short_gross = -weights.clip(upper=0).sum(axis=1).shift(1).fillna(0.0)
    held_count = (weights != 0).sum(axis=1)
    return OverlayResult(
        returns,
        short_gross,
        weights,
        stop_exits=stop_exits,
        loss_exits=loss_exits,
        blocked_selections=blocked_selections,
        diagnostics={
            # Averaged over invested days only -- including the 252+21 warmup,
            # when weights are all zero, would understate this by ~3x.
            "mean_names_held": float(held_count[held_count > 0].mean()),
            "annual_turnover": float(turnover.mean() * TD),
        },
    )


VARIANTS = {
    # The construction production_portfolio.py validated.
    "control (no stop, no block)": dict(stop_mode="none", block_days=0),
    # What is actually deployed.
    "live (ATR stop + 5d block)": dict(stop_mode="atr", block_days=5),
    # Each control in isolation, to attribute any difference.
    "ATR stop only": dict(stop_mode="atr", block_days=0),
    "5d block only": dict(stop_mode="none", block_days=5),
    # Proxy-independent brackets.
    "flat 8% stop + 5d block": dict(stop_mode="flat", flat_stop=0.08, block_days=5),
    "flat 15% stop + 5d block": dict(stop_mode="flat", flat_stop=0.15, block_days=5),
}


def main() -> None:
    close, volume = load()
    close, volume = norm_index(close), norm_index(volume)
    classified = json.loads(Path("state/universe_classified.json").read_text())
    stocks = [s for s in classified["stocks"] if s in close.columns]
    close, volume = close[stocks], volume[stocks]

    streams = build_streams()
    common = (
        0.40 * streams["spy"]
        + 0.25 * streams["tsmom"]
        + 0.20 * streams["trend"]
    )

    results: dict[str, OverlayResult] = {}
    for name, kwargs in VARIANTS.items():
        print(f"  simulating {name} ...", flush=True)
        results[name] = build_overlay_stream(close, volume, **kwargs)

    performance: dict[str, list] = {}
    for window, slicer in WINDOWS.items():
        performance[window] = []
        for profile in ("base", "2x"):
            for name, result in results.items():
                stream = profile_returns(common, result, profile=profile)
                sliced = stream.loc[slicer].dropna()
                if sliced.empty:
                    continue
                row = returns_summary(sliced, f"{profile} — {name}")
                row.update(
                    profile=profile,
                    variant=name,
                    stop_exits=result.stop_exits,
                    loss_exits=result.loss_exits,
                    blocked_selections=result.blocked_selections,
                    **result.diagnostics,
                )
                performance[window].append(row)

    # Verdict: the live overlay has to beat the construction that was actually
    # validated, in both profiles and both windows, to justify staying on.
    #
    # 2026-08-03 re-audit fix: this used to compute live_ever_better = any(...)
    # over the 4 cells while this very comment said "both profiles and both
    # windows" (i.e. all 4), and it compared only sharpe/cagr despite
    # collecting max_dd. On the data at the time neither bug changed the
    # decision (any() was already false, and all() implies any()), but it
    # was the wrong check and could have. Now uses
    # backtest.promotion.passes_gate_all_cells (return_enhancer: sharpe
    # higher, cagr and max_dd not worse) requiring every one of the 4 cells
    # to pass before keeping the live overlay.
    control = "control (no stop, no block)"
    live = "live (ATR stop + 5d block)"
    cells = []
    for window in WINDOWS:
        for profile in ("base", "2x"):
            rows = {r["variant"]: r for r in performance[window] if r["profile"] == profile}
            if control not in rows or live not in rows:
                continue
            cells.append((window, profile, rows[control], rows[live]))
    gate = passes_gate_all_cells(cells, "return_enhancer")
    checks = [
        {
            "window": window,
            "profile": profile,
            "control_sharpe": control_row["sharpe"],
            "live_sharpe": live_row["sharpe"],
            "control_cagr": control_row["cagr"],
            "live_cagr": live_row["cagr"],
            "control_max_dd": control_row["max_dd"],
            "live_max_dd": live_row["max_dd"],
            "live_better": gate_cell["passed"],
        }
        for (window, profile, control_row, live_row), gate_cell in zip(cells, gate["cells"])
    ]

    decision = "keep_live_overlay" if gate["passed"] else "remove_overlay_from_mom_ls"

    out = {
        "decision": decision,
        "question": (
            "Production adds a stop-loss and a 5-day loss re-entry block to the "
            "MOM_LS sleeve. production_portfolio.py models neither. Do they help?"
        ),
        "control_is_production_backtest": True,
        "gate_2026_08_03": gate,
        "reexamination_note_2026_08_03": (
            "Before the panel/gate fixes, held-out Sharpe and drawdown both "
            "favored the live overlay in both profiles, while only the early "
            "(COVID-containing) window supported removal - the quantitative "
            "case rested on a window later found to have no COVID-crash "
            "coverage at all. On the corrected panel, that ambiguity is gone: "
            "the live overlay now has lower Sharpe AND lower CAGR than "
            "control in all 4 cells, not just early_2020_2022. Max drawdown "
            "is still mixed (worse with the overlay in the early window, "
            "better in held-out), but Sharpe and CAGR no longer disagree "
            "across windows. The removal decision is unchanged but now rests "
            "on more consistent evidence than it did in the 2026-08-03 "
            "campaign that made it."
        ),
        "verdict_checks": checks,
        "performance": performance,
        "limitations": [
            "ATR14 is unavailable in the cross-sectional cache; the ATR variant "
            "uses an inflated close-to-close proxy and is bracketed by the flat "
            "8% and 15% variants.",
            "Stops are evaluated on closes; whole-share short stops can trigger "
            "intraday in production, so short stop-outs are an optimistic bound.",
            "Historical easy-to-borrow and locate availability is unavailable.",
            "The universe of currently listed companies is survivorship-biased.",
        ],
    }
    path = Path("reports/risk_overlay_study.json")
    path.write_text(json.dumps(out, indent=2, default=float))
    print(f"wrote {path}")
    print(f"decision: {decision}")


if __name__ == "__main__":
    main()
