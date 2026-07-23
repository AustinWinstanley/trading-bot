"""Pre-2020 stress proxy for the production portfolio.

SPY, trend, and the configured 15-asset TSMOM sleeve are reconstructed from
adjusted ETF history. The individual-stock MOM_LS sleeve cannot honestly be
recreated before the point-in-time stock matrix, so this study uses Kenneth
French's daily U.S. momentum factor as a clearly labeled proxy.

The factor is volatility-scaled during its overlap with each capacity-adjusted
paper profile. Its mean is not fitted. This preserves historical momentum
crashes while avoiding the fiction that a broad value-weighted academic factor
is the same strategy as our equal-weight top/bottom-20 implementation.
"""

from __future__ import annotations

import json
import re
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd

from backtest.production_portfolio import (
    MARGIN_RATE,
    SHORT_BORROW,
    TD,
    build_streams,
    norm_index,
    returns_summary,
)
from backtest.short_capacity_study import (
    MOM_ACCOUNT_MULTIPLIER,
    solve_dynamic_equity,
)
from backtest.xsec_data import load
from engine.config import load_config
from engine.tiingo import load_parquet

FRENCH_ZIP = Path("state/french/F-F_Momentum_Factor_daily_CSV.zip")
ASSET_HISTORY = Path("state/history_assets")
DEEP_HISTORY = Path("state/history_deep")


def parse_french_momentum(text: str) -> pd.Series:
    """Parse the dated percentage rows from the French CSV payload."""
    observations = {}
    for line in text.splitlines():
        match = re.match(r"^(\d{8}),\s*(-?\d+(?:\.\d+)?)", line.strip())
        if not match:
            continue
        value = float(match.group(2))
        if value <= -99:
            continue
        observations[pd.Timestamp(match.group(1))] = value / 100.0
    if not observations:
        raise ValueError("no momentum observations found")
    return pd.Series(observations, name="french_mom").sort_index()


def load_french_momentum(path: Path = FRENCH_ZIP) -> pd.Series:
    with zipfile.ZipFile(path) as archive:
        names = [name for name in archive.namelist() if name.lower().endswith(".csv")]
        if len(names) != 1:
            raise ValueError(f"expected one CSV in {path}, found {names}")
        text = archive.read(names[0]).decode("utf-8", errors="replace")
    return parse_french_momentum(text)


def long_history_common(min_assets: int = 8) -> pd.DataFrame:
    """Exact SPY, trend, and TSMOM components where enough assets exist."""
    cfg = load_config()
    symbols = cfg.sleeves_paper["tsmom_universe"]
    frames = load_parquet(symbols, ASSET_HISTORY)
    missing = sorted(set(symbols) - set(frames))
    if missing:
        raise FileNotFoundError(f"missing TSMOM histories: {missing}")
    close = pd.DataFrame({
        symbol: norm_index(frame["close"])
        for symbol, frame in frames.items()
    })
    returns = close.pct_change(fill_method=None)
    has_lookback = close.shift(252).notna()
    signal = (close / close.shift(252) - 1.0).gt(0) & has_lookback
    inverse_vol = 1 / (
        returns.rolling(63, min_periods=40).std() * np.sqrt(TD)
    ).clip(lower=0.04)
    weights = signal * inverse_vol
    weights = weights.div(weights.sum(axis=1).replace(0, np.nan), axis=0)
    weights = weights.fillna(0.0).shift(1).fillna(0.0)
    tsmom = (weights * returns).sum(axis=1)
    tsmom -= weights.diff().abs().sum(axis=1) * 8 / 10_000

    spy_frame = load_parquet(["SPY"], DEEP_HISTORY)["SPY"]
    spy = norm_index(spy_frame["close"]).reindex(close.index).ffill()
    spy_return = spy.pct_change(fill_method=None)
    trend_on = (spy.shift(1) > spy.shift(1).rolling(200).mean()).astype(float)
    trend = trend_on * spy_return
    eligible_assets = has_lookback.sum(axis=1)
    result = pd.DataFrame({
        "spy": spy_return,
        "tsmom": tsmom,
        "trend": trend,
        "eligible_assets": eligible_assets,
    })
    result = result[result["eligible_assets"] >= min_assets].dropna()
    result["common"] = (
        0.40 * result["spy"]
        + 0.25 * result["tsmom"]
        + 0.20 * result["trend"]
    )
    return result


def volatility_scale(proxy: pd.Series, target: pd.Series) -> tuple[float, float]:
    aligned = pd.concat(
        {"proxy": proxy, "target": target}, axis=1, sort=False
    ).dropna()
    if len(aligned) < 20 or aligned["proxy"].std() <= 0:
        raise ValueError("insufficient overlap for volatility calibration")
    scale = float(aligned["target"].std() / aligned["proxy"].std())
    correlation = float(aligned["target"].corr(aligned["proxy"]))
    return scale, correlation


def stress_window(r: pd.Series, start: str, end: str, label: str) -> dict:
    sample = r.loc[start:end].dropna()
    if sample.empty:
        return {"window": label, "error": "no observations"}
    equity = (1 + sample).cumprod()
    peaks = equity.cummax().clip(lower=1.0)
    return {
        "window": label,
        "from": sample.index[0].date().isoformat(),
        "to": sample.index[-1].date().isoformat(),
        "sessions": len(sample),
        "total_return": round(float(equity.iloc[-1] - 1), 4),
        "max_drawdown": round(float((equity / peaks - 1).min()), 4),
        "worst_day": round(float(sample.min()), 4),
    }


def main() -> None:
    french = load_french_momentum()
    history = long_history_common()

    current_close, current_volume = load()
    current_close, current_volume = (
        norm_index(current_close),
        norm_index(current_volume),
    )
    classified = json.loads(Path("state/universe_classified.json").read_text())
    stocks = [symbol for symbol in classified["stocks"] if symbol in current_close]
    current_close, current_volume = (
        current_close[stocks],
        current_volume[stocks],
    )
    production = build_streams()
    current_common = (
        0.40 * production["spy"]
        + 0.25 * production["tsmom"]
        + 0.20 * production["trend"]
    )

    calibration = {}
    variants = {}
    actual_profiles = {}
    capacity_report = json.loads(
        Path("reports/short_capacity_study.json").read_text()
    )
    capacity_rows = capacity_report["capacity"]
    for profile in ("base", "2x"):
        actual_profile, capacity = solve_dynamic_equity(
            current_close,
            current_volume,
            current_common,
            profile=profile,
            selection="ranked",
            short_n=20,
        )
        multiplier = MOM_ACCOUNT_MULTIPLIER[profile]
        actual_momentum = multiplier * capacity.returns
        scale, correlation = volatility_scale(french, actual_momentum)
        capacity_row = next(
            row for row in capacity_rows
            if row["profile"] == profile
            and row["variant"] == "current whole-share bottom 20"
        )
        short_gross = float(capacity_row["average_realized_short_gross"])
        momentum_proxy = french * scale - short_gross * SHORT_BORROW / TD
        aligned = pd.concat(
            {
                "common": history["common"] * (2 if profile == "2x" else 1),
                "momentum": momentum_proxy,
            },
            axis=1,
            sort=False,
        ).dropna()
        financing = MARGIN_RATE / TD if profile == "2x" else 0.0
        variants[profile] = aligned.sum(axis=1) - financing
        actual_profiles[profile] = actual_profile
        overlap = pd.concat(
            {"actual": actual_momentum, "proxy": french * scale},
            axis=1,
            sort=False,
        ).dropna()
        calibration[profile] = {
            "volatility_scale": round(scale, 4),
            "overlap_correlation": round(correlation, 4),
            "overlap_from": overlap.index[0].date().isoformat(),
            "overlap_to": overlap.index[-1].date().isoformat(),
            "actual_momentum_ann_vol": round(
                float(overlap["actual"].std() * np.sqrt(TD)), 4
            ),
            "scaled_proxy_ann_vol": round(
                float(overlap["proxy"].std() * np.sqrt(TD)), 4
            ),
            "average_short_gross": short_gross,
        }

    windows = {
        "GFC": ("2007-10-09", "2009-03-09"),
        "momentum crash rebound": ("2009-03-10", "2009-06-30"),
        "Eurozone / US downgrade": ("2011-05-02", "2011-10-03"),
        "China / energy selloff": ("2015-07-01", "2016-02-11"),
        "2018 volatility and Q4": ("2018-01-26", "2018-12-24"),
        "COVID crash": ("2020-02-19", "2020-03-23"),
        "inflation bear": ("2022-01-03", "2022-10-12"),
    }
    stress = {}
    for profile, returns in variants.items():
        stress[profile] = [
            stress_window(returns, start, end, label)
            for label, (start, end) in windows.items()
        ]

    payload = {
        "conclusion": (
            "Keep both profiles in paper, and do not promote 2x to real "
            "capital on current evidence. The long-history proxy adds little "
            "CAGR at 2x while nearly doubling drawdown. The academic momentum "
            "proxy extends regime coverage but is not validation of the "
            "deployed top/bottom-20 sleeve."
        ),
        "source": {
            "provider": "Kenneth R. French Data Library",
            "dataset": "Daily Momentum Factor (Mom)",
            "url": (
                "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/"
                "Data_Library/det_mom_factor_daily.html"
            ),
            "construction": (
                "Value-weighted high-minus-low prior 2-12 month return "
                "portfolios across NYSE, AMEX, and Nasdaq."
            ),
        },
        "limitations": [
            "French Mom is broad and value-weighted; production is equal-weight top/bottom 20.",
            "Volatility is matched on 2020-2026 overlap; mean return is not fitted.",
            "Historical whole-share capacity is represented by average observed capacity.",
            "Historical borrowability, live slippage tails, and outages remain unavailable.",
            "The TSMOM universe begins with eight available assets and grows to fifteen.",
        ],
        "history": {
            "from": min(series.index[0] for series in variants.values()).date().isoformat(),
            "to": max(series.index[-1] for series in variants.values()).date().isoformat(),
            "tsmom_min_assets": 8,
            "tsmom_max_assets": int(history["eligible_assets"].max()),
        },
        "calibration": calibration,
        "full_period": [
            returns_summary(series, f"{profile} long-history proxy")
            for profile, series in variants.items()
        ],
        "stress_windows": stress,
    }
    out = Path("reports/long_history_stress_study.json")
    out.write_text(json.dumps(payload, indent=2))

    print("CALIBRATION")
    print(pd.DataFrame(calibration).T.to_string())
    print("\nFULL PERIOD")
    print(pd.DataFrame(payload["full_period"]).to_string(index=False))
    for profile, rows in stress.items():
        print(f"\n{profile} STRESS WINDOWS")
        print(
            pd.DataFrame(rows)[
                ["window", "total_return", "max_drawdown", "worst_day"]
            ].to_string(index=False)
        )
    print(f"\nWrote {out}")


if __name__ == "__main__":
    main()
