"""Delisting-aware bounds for the deployed cross-sectional momentum sleeve.

The original matrix contains currently listed companies only. This study adds
historical U.S. listings that disappeared during 2020-2025 and tests several
explicit terminal-return assumptions. Historical borrow availability remains
unknown, so even the extended result is evidence rather than validation.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from backtest.production_portfolio import (
    MARGIN_RATE,
    SHORT_BORROW,
    build_streams,
    norm_index,
    returns_summary,
)
from backtest.xsec_data import load
from backtest.xsec_momentum import build_portfolio

TD = 252
DELISTED_DIR = Path("state/xsec_delisted")


def merge_universes() -> tuple[pd.DataFrame, pd.DataFrame, list[str]]:
    current_close, current_volume = load()
    classified = json.loads(
        Path("state/universe_classified.json").read_text()
    )
    current_symbols = [
        symbol for symbol in classified["stocks"] if symbol in current_close
    ]
    current_close = current_close[current_symbols]
    current_volume = current_volume[current_symbols]
    ended_close = pd.read_parquet(DELISTED_DIR / "close.parquet")
    ended_volume = pd.read_parquet(DELISTED_DIR / "volume.parquet")
    current_close, current_volume = norm_index(current_close), norm_index(current_volume)
    ended_close, ended_volume = norm_index(ended_close), norm_index(ended_volume)
    ended_symbols = [
        symbol for symbol in ended_close if symbol not in current_close.columns
    ]
    close = pd.concat(
        [current_close, ended_close[ended_symbols]], axis=1, sort=False
    ).sort_index()
    volume = pd.concat(
        [current_volume, ended_volume[ended_symbols]], axis=1, sort=False
    ).reindex(close.index)
    return close, volume, ended_symbols


def terminal_overrides(
    close: pd.DataFrame,
    symbols: list[str],
    *,
    mode: str,
) -> pd.DataFrame:
    """Sparse next-session returns for symbols whose observed history ends."""
    overrides = pd.DataFrame(index=close.index, columns=symbols, dtype="float32")
    for symbol in symbols:
        series = close[symbol].dropna()
        if len(series) < 2 or series.index[-1] >= close.index[-1]:
            continue
        next_locations = close.index[close.index > series.index[-1]]
        if not len(next_locations):
            continue
        terminal_return = 0.0
        if mode == "all_minus_30pct":
            terminal_return = -0.30
        elif mode == "all_zero":
            terminal_return = -1.0
        elif mode == "distress_to_zero":
            trailing_peak = float(series.tail(252).max())
            terminal_return = (
                -1.0 if trailing_peak > 0 and float(series.iloc[-1]) / trailing_peak <= 0.20
                else 0.0
            )
        elif mode != "observed_last_price":
            raise ValueError(f"unknown terminal mode {mode!r}")
        if terminal_return:
            overrides.at[next_locations[0], symbol] = terminal_return
    return overrides


def momentum_stream(
    close: pd.DataFrame,
    volume: pd.DataFrame,
    overrides: pd.DataFrame | None,
) -> pd.Series:
    equity, _ = build_portfolio(
        close,
        volume,
        lookback=252,
        skip=21,
        top_n=20,
        rebalance=5,
        min_price=5.0,
        min_dollar_volume=5e6,
        cost_bps=15,
        short_bottom=True,
        return_overrides=overrides,
    )
    return equity.pct_change()


def result_windows(variants: dict[str, pd.Series]) -> dict[str, list[dict]]:
    result = {}
    for window, slicer in {
        "full": slice(None),
        "early_2020_2022": slice(None, "2022-12-31"),
        "heldout_2023_plus": slice("2023-01-01", None),
    }.items():
        result[window] = [
            returns_summary(stream.loc[slicer], name)
            for name, stream in variants.items()
        ]
    return result


def main() -> None:
    current_close, current_volume = load()
    classified = json.loads(Path("state/universe_classified.json").read_text())
    current_symbols = [
        symbol for symbol in classified["stocks"] if symbol in current_close
    ]
    current_close = current_close[current_symbols]
    current_volume = current_volume[current_symbols]
    current_close, current_volume = (
        norm_index(current_close),
        norm_index(current_volume),
    )
    close, volume, ended_symbols = merge_universes()
    print(
        f"Current universe {current_close.shape[1]:,}; "
        f"added delisted {len(ended_symbols):,}; combined {close.shape[1]:,}"
    )

    variants = {
        "survivors only": momentum_stream(
            current_close, current_volume, overrides=None
        )
    }
    terminal_counts = {}
    for mode in (
        "observed_last_price",
        "distress_to_zero",
        "all_minus_30pct",
        "all_zero",
    ):
        overrides = terminal_overrides(close, ended_symbols, mode=mode)
        terminal_counts[mode] = int(overrides.notna().sum().sum())
        variants[f"extended: {mode}"] = momentum_stream(close, volume, overrides)

    standalone = result_windows(variants)

    production = {}
    streams = build_streams()
    common = (
        0.40 * streams["spy"]
        + 0.25 * streams["tsmom"]
        + 0.20 * streams["trend"]
    )
    for name, momentum in variants.items():
        aligned = pd.DataFrame({"common": common, "momentum": momentum}).dropna()
        raw = aligned["common"] + 0.30 * aligned["momentum"]
        production[f"base — {name}"] = raw - 0.15 * SHORT_BORROW / TD
        production[f"2x — {name}"] = (
            2 * raw - MARGIN_RATE / TD - 0.30 * SHORT_BORROW / TD
        )
    production_results = result_windows(production)

    payload = {
        "conclusion": (
            "MOM_LS survives a delisting-aware universe but its estimate is "
            "lower. Keep the paper sleeve; treat survivor-only results as the "
            "upper bound and universal-zero terminal results as a deliberately "
            "severe lower bound."
        ),
        "limitations": [
            "Historical short borrowability is unavailable.",
            "Tiingo metadata classifies some corporate shells as stocks.",
            "Alpaca IEX delisted coverage begins in mid-2020.",
            "Terminal scenarios are bounds, not known delisting proceeds.",
        ],
        "universe": {
            "survivors": current_close.shape[1],
            "delisted_added": len(ended_symbols),
            "combined": close.shape[1],
            "terminal_loss_events": terminal_counts,
        },
        "mom_ls": standalone,
        "production_portfolio": production_results,
    }
    out = Path("reports/survivorship_study.json")
    out.write_text(json.dumps(payload, indent=2))

    for title, results in (
        ("MOM_LS", standalone),
        ("PRODUCTION PORTFOLIO", production_results),
    ):
        for window, rows in results.items():
            print(f"\n{title} — {window}")
            print(
                pd.DataFrame(rows)[
                    ["portfolio", "cagr", "sharpe", "max_dd"]
                ].to_string(index=False)
            )
    print("\nterminal events:", terminal_counts)
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
