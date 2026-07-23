"""Block-bootstrap outcome ranges for the capacity-adjusted paper profiles.

Point-estimate CAGRs hide how short the 2020-2026 production sample is.  This
study resamples contiguous return blocks so volatility clustering and short
market regimes survive within each block.  It reports one- and three-year
outcome distributions for the current whole-share construction.

The severe variant also subtracts the annualized return gap between the
survivor-only and universal-zero delisting scenarios.  That overlay is a
deliberately harsh sensitivity test, not a forecast or confidence interval.
Bootstrap paths cannot contain market regimes absent from the source sample.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from backtest.production_portfolio import build_streams, norm_index, returns_summary
from backtest.short_capacity_study import solve_dynamic_equity
from backtest.xsec_data import load

TD = 252


def circular_block_indices(
    observations: int,
    horizon: int,
    *,
    block_size: int,
    simulations: int,
    rng: np.random.Generator,
) -> np.ndarray:
    """Sample circular contiguous blocks and trim them to ``horizon``."""
    if observations < 1 or horizon < 1 or block_size < 1 or simulations < 1:
        raise ValueError("all dimensions must be positive")
    blocks = int(np.ceil(horizon / block_size))
    starts = rng.integers(0, observations, size=(simulations, blocks))
    offsets = np.arange(block_size)
    return ((starts[..., None] + offsets) % observations).reshape(
        simulations, -1
    )[:, :horizon]


def bootstrap_outcomes(
    returns: pd.Series,
    *,
    years: int,
    block_size: int = 63,
    simulations: int = 5_000,
    seed: int = 20260723,
    chunk_size: int = 250,
) -> dict:
    """Return a reproducible distribution without retaining every path."""
    values = returns.dropna().to_numpy(dtype=float)
    horizon = years * TD
    rng = np.random.default_rng(seed)
    cagrs: list[np.ndarray] = []
    drawdowns: list[np.ndarray] = []
    sharpes: list[np.ndarray] = []
    remaining = simulations
    while remaining:
        batch = min(chunk_size, remaining)
        indices = circular_block_indices(
            len(values),
            horizon,
            block_size=block_size,
            simulations=batch,
            rng=rng,
        )
        sampled = values[indices]
        equity = np.cumprod(1.0 + sampled, axis=1)
        # Include starting capital in the high-water mark. Without the 1.0
        # floor, a loss on the first sampled day incorrectly starts at 0% DD.
        peaks = np.maximum.accumulate(np.maximum(equity, 1.0), axis=1)
        cagrs.append(equity[:, -1] ** (1.0 / years) - 1.0)
        drawdowns.append(np.min(equity / peaks - 1.0, axis=1))
        annual_vol = sampled.std(axis=1, ddof=1) * np.sqrt(TD)
        sharpes.append(
            np.divide(
                sampled.mean(axis=1) * TD,
                annual_vol,
                out=np.zeros(batch),
                where=annual_vol > 0,
            )
        )
        remaining -= batch

    cagr = np.concatenate(cagrs)
    drawdown = np.concatenate(drawdowns)
    sharpe = np.concatenate(sharpes)

    def quantiles(values: np.ndarray) -> dict:
        return {
            f"p{q:02d}": round(float(np.percentile(values, q)), 4)
            for q in (5, 25, 50, 75, 95)
        }

    return {
        "years": years,
        "block_sessions": block_size,
        "simulations": simulations,
        "cagr": quantiles(cagr),
        "sharpe": quantiles(sharpe),
        "max_drawdown": quantiles(drawdown),
        "probability_negative_cagr": round(float((cagr < 0).mean()), 4),
        "probability_drawdown_over_20pct": round(
            float((drawdown <= -0.20).mean()), 4
        ),
        "probability_drawdown_over_30pct": round(
            float((drawdown <= -0.30).mean()), 4
        ),
    }


def severe_delisting_drags(path: Path) -> dict[str, float]:
    """Annual-return haircut from survivor-only to universal-zero bounds."""
    payload = json.loads(path.read_text())
    rows = payload["production_portfolio"]["full"]
    output = {}
    for profile in ("base", "2x"):
        survivor = next(
            row for row in rows
            if row["portfolio"] == f"{profile} — survivors only"
        )
        severe = next(
            row for row in rows
            if row["portfolio"] == f"{profile} — extended: all_zero"
        )
        output[profile] = float(survivor["ann_return"] - severe["ann_return"])
    return output


def main() -> None:
    close_all, volume_all = load()
    close_all, volume_all = norm_index(close_all), norm_index(volume_all)
    classified = json.loads(Path("state/universe_classified.json").read_text())
    stocks = [symbol for symbol in classified["stocks"] if symbol in close_all]
    close, volume = close_all[stocks], volume_all[stocks]

    production = build_streams()
    common = (
        0.40 * production["spy"]
        + 0.25 * production["tsmom"]
        + 0.20 * production["trend"]
    )
    drags = severe_delisting_drags(Path("reports/survivorship_study.json"))
    variants = {}
    for profile in ("base", "2x"):
        current, _ = solve_dynamic_equity(
            close,
            volume,
            common,
            profile=profile,
            selection="ranked",
            short_n=20,
        )
        variants[f"{profile} current"] = current
        variants[f"{profile} severe-delisting overlay"] = (
            current - drags[profile] / TD
        )

    empirical = [
        returns_summary(series, label) for label, series in variants.items()
    ]
    bootstrap = {}
    for label, series in variants.items():
        print(f"Bootstrapping {label}...")
        bootstrap[label] = {
            f"{years}y": bootstrap_outcomes(series, years=years)
            for years in (1, 3)
        }
    block_sensitivity = {}
    for label, series in variants.items():
        if "severe-delisting" not in label:
            continue
        block_sensitivity[label] = {
            str(block): (
                bootstrap[label]["3y"] if block == 63 else
                bootstrap_outcomes(series, years=3, block_size=block)
            )
            for block in (21, 63, 126)
        }

    payload = {
        "conclusion": (
            "Use ranges rather than headline CAGR as the planning estimate. "
            "The bootstrap quantifies sampling uncertainty but remains "
            "conditional on the unusually short 2020-2026 history."
        ),
        "method": {
            "sampling": "circular moving-block bootstrap",
            "block_sessions": 63,
            "block_sensitivity_sessions": [21, 63, 126],
            "simulations": 5_000,
            "seed": 20260723,
            "delisting_overlay_annual_return_drag": drags,
        },
        "limitations": [
            "Resampling cannot create crashes or inflation regimes absent from 2020-2026.",
            "The source universe remains survivorship-biased before the stress overlay.",
            "Historical borrowability, slippage tails, taxes, and outages are unavailable.",
            "Percentiles are scenario ranges, not calibrated forecast probabilities.",
        ],
        "empirical": empirical,
        "bootstrap": bootstrap,
        "three_year_block_size_sensitivity": block_sensitivity,
    }
    out = Path("reports/return_uncertainty_study.json")
    out.write_text(json.dumps(payload, indent=2))

    print("\nEMPIRICAL")
    print(
        pd.DataFrame(empirical)[
            ["portfolio", "cagr", "sharpe", "max_dd"]
        ].to_string(index=False)
    )
    for label, horizons in bootstrap.items():
        print(f"\n{label}")
        for horizon, result in horizons.items():
            cagr = result["cagr"]
            dd = result["max_drawdown"]
            print(
                f"  {horizon}: CAGR p05/50/95 "
                f"{cagr['p05']:.1%}/{cagr['p50']:.1%}/{cagr['p95']:.1%}; "
                f"DD p05/50 {dd['p05']:.1%}/{dd['p50']:.1%}; "
                f"P(loss) {result['probability_negative_cagr']:.1%}"
            )
    print(f"\nWrote {out}")


if __name__ == "__main__":
    main()
