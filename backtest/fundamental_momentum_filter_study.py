"""Fundamental filters on cross-sectional momentum, run through the full gate.

Two candidates, both pre-registered before looking at results, both applying
the same mechanical filter documented in
`reports/quality_momentum_filter_feasibility.json`: remove the bottom-quintile
names (by the given score) from the long candidate pool and the top-quintile
names from the short candidate pool, then continue down the *unchanged*
momentum rank to fill 20 names per leg. Momentum order is never re-ranked —
only which names are eligible changes.

  quality candidate   score = equal-weight cross-sectional rank of
                       gross-profit/assets and operating-cash-flow/assets
                       (the feasibility contract's exact definition)
  accruals candidate  score = -(NetIncome - OperatingCashFlow) / assets
                       (Sloan 1996; the signal reports/fund_signals.json
                       found sign-stable and near-orthogonal to momentum)

Both use the fixed point-in-time fundamentals cache (trailing-4-quarter flows,
filed+1 availability) from `backtest.fund_signals`, and both inherit its
documented CIK-map survivorship bias: `engine.fundamentals.cik_map()` is
current-listing only and drops ~45% of CIKs with fundamental facts. Coverage
is measured and reported per the feasibility contract's >=80% requirement —
expect it to be far short of that, since 45% CIK loss alone puts an upper
bound on achievable coverage.

Control reproduces `xsec_momentum.build_portfolio` exactly (same function,
unmodified) per AGENTS.md's validation discipline.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from backtest.fund_signals import load_fundamental_matrices, cik_map_coverage
from backtest.production_portfolio import build_streams, norm_index, returns_summary
from backtest.promotion import passes_gate, passes_gate_all_cells
from backtest.short_capacity_study import MOM_ACCOUNT_MULTIPLIER, STARTING_EQUITY, profile_returns
from backtest.xsec_data import load
from backtest.xsec_momentum import build_portfolio
from engine.fundamentals import cik_map

TD = 252
EXCLUDE_QUANTILE = 0.20  # bottom/top quintile, per the pre-registered contract


def build_scores(close: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """Point-in-time quality and accruals score matrices, aligned to `close`."""
    # fund_signals.load_fundamental_matrices builds avail_date as tz-aware
    # UTC; production_portfolio.norm_index (used on `close` in this file)
    # strips tz to naive. Match its expectation rather than `close`'s.
    dates = close.index
    if dates.tz is None:
        dates = dates.tz_localize("UTC")
    f = load_fundamental_matrices(dates)
    for key, m in f.items():
        if m.index.tz is not None:
            m.index = m.index.tz_localize(None)
        f[key] = m

    def A(m):
        return m.reindex(columns=close.columns)

    ni, ocf = A(f["net_income"]), A(f["ocf"])
    gp, assets = A(f["gross_profit"]), A(f["assets"])

    gp_ratio = gp / assets.replace(0, np.nan)
    ocf_ratio = ocf / assets.replace(0, np.nan)
    quality = gp_ratio.rank(axis=1, pct=True) + ocf_ratio.rank(axis=1, pct=True)

    accruals = -(ni - ocf) / assets.replace(0, np.nan)

    return {"quality": quality, "accruals": accruals}


def build_portfolio_with_fundamental_filter(
    close: pd.DataFrame,
    volume: pd.DataFrame,
    score: pd.DataFrame,
    *,
    lookback: int = 252,
    skip: int = 21,
    top_n: int = 20,
    rebalance: int = 5,
    min_price: float = 5.0,
    min_dollar_volume: float = 5e6,
    cost_bps: float = 15.0,
    exclude_quantile: float = EXCLUDE_QUANTILE,
) -> tuple[pd.Series, pd.DataFrame]:
    """Same mechanics as `xsec_momentum.build_portfolio`, plus the filter.

    Momentum ranking, eligibility, and cost accounting are copied verbatim
    from `build_portfolio` rather than imported and wrapped, so a change
    there can't silently desync this from what it's being compared against —
    the reproduction check in `main()` is what actually guards that.
    """
    dollar_volume = (close * volume).rolling(20, min_periods=10).mean()
    past = close.shift(lookback)
    recent = close.shift(skip)
    momentum = (recent / past) - 1.0
    eligible = (
        close.shift(skip).gt(min_price)
        & dollar_volume.shift(skip).gt(min_dollar_volume)
        & momentum.notna()
        & close.notna()
    )
    daily_returns = close.pct_change()
    rebalance_days = close.index[lookback + skip :: rebalance]

    weights = pd.DataFrame(0.0, index=close.index, columns=close.columns, dtype="float32")
    holdings_log = []

    for date in rebalance_days:
        mom = momentum.loc[date].where(eligible.loc[date]).dropna().sort_values(ascending=False)
        if len(mom) < top_n * 2:
            continue
        sc = score.loc[date].reindex(mom.index) if date in score.index else pd.Series(np.nan, index=mom.index)
        covered = sc.notna()
        coverage_pct = float(covered.mean()) if len(covered) else 0.0
        if covered.sum() >= 20:
            bottom_thresh = float(sc[covered].quantile(exclude_quantile))
            top_thresh = float(sc[covered].quantile(1 - exclude_quantile))
        else:
            bottom_thresh, top_thresh = -np.inf, np.inf

        longs, shorts = [], []
        for sym in mom.index:
            if len(longs) >= top_n:
                break
            s = sc.get(sym, np.nan)
            if pd.notna(s) and s < bottom_thresh:
                continue
            longs.append(sym)
        for sym in mom.index[::-1]:
            if len(shorts) >= top_n:
                break
            s = sc.get(sym, np.nan)
            if pd.notna(s) and s > top_thresh:
                continue
            shorts.append(sym)

        w = pd.Series(0.0, index=close.columns, dtype="float32")
        w[longs] = 1.0 / top_n
        w[shorts] = -1.0 / top_n
        w /= 2.0
        weights.loc[date:] = w.values
        holdings_log.append({
            "date": date,
            "n_eligible": int(len(mom)),
            "coverage_pct": round(coverage_pct, 4),
        })

    port_returns = (weights.shift(1) * daily_returns).sum(axis=1)
    turnover = weights.diff().abs().sum(axis=1)
    port_returns -= turnover * (cost_bps / 10_000.0)
    equity = (1 + port_returns.fillna(0)).cumprod()
    return equity, pd.DataFrame(holdings_log)


def solve(
    close: pd.DataFrame,
    volume: pd.DataFrame,
    common: pd.Series,
    *,
    profile: str,
    score: pd.DataFrame | None,
) -> tuple[pd.Series, pd.DataFrame]:
    equity = pd.Series(STARTING_EQUITY, index=close.index)
    holdings_log = pd.DataFrame()
    portfolio = pd.Series(dtype=float)
    for _ in range(3):
        if score is None:
            mom_equity, holdings_log = build_portfolio(
                close, volume, lookback=252, skip=21, top_n=20, rebalance=5,
                min_price=5.0, min_dollar_volume=5e6, cost_bps=15.0, short_bottom=True,
            )
        else:
            mom_equity, holdings_log = build_portfolio_with_fundamental_filter(
                close, volume, score,
            )
        mom_returns = mom_equity.pct_change()
        multiplier = MOM_ACCOUNT_MULTIPLIER[profile]
        aligned = pd.concat({"common": common, "momentum": mom_returns}, axis=1, sort=False).dropna()
        portfolio = aligned["common"] + aligned["momentum"] * multiplier
        equity = STARTING_EQUITY * (1 + portfolio).cumprod().reindex(close.index).ffill().fillna(1)
    return portfolio, holdings_log


def main() -> None:
    close_all, volume_all = load()
    close_all, volume_all = norm_index(close_all), norm_index(volume_all)
    classified = json.loads(Path("state/universe_classified.json").read_text())
    stocks = [s for s in classified["stocks"] if s in close_all.columns]
    close, volume = close_all[stocks], volume_all[stocks]

    # Reproduction check (AGENTS.md discipline): this filter function with an
    # all-pass score (never excludes anyone) must match build_portfolio.
    never_exclude = pd.DataFrame(0.5, index=close.index, columns=close.columns)
    check_equity, _ = build_portfolio_with_fundamental_filter(
        close, volume, never_exclude, exclude_quantile=0.0
    )
    reference_equity, _ = build_portfolio(
        close, volume, lookback=252, skip=21, top_n=20, rebalance=5,
        min_price=5.0, min_dollar_volume=5e6, cost_bps=15.0, short_bottom=True,
    )
    reproduction_max_abs_diff = float((check_equity - reference_equity).abs().max())

    print("Building fundamental score matrices...")
    scores = build_scores(close)
    fund_df = pd.read_parquet(
        next((Path("state/fundamentals")).glob("fundamentals_*.parquet"))
    )
    coverage = cik_map_coverage(fund_df, cik_map())

    production = build_streams()
    common = 0.40 * production["spy"] + 0.25 * production["tsmom"] + 0.20 * production["trend"]

    portfolios: dict[tuple[str, str], pd.Series] = {}
    logs: dict[tuple[str, str], pd.DataFrame] = {}
    for profile in ("base", "2x"):
        print(f"Running {profile}: control...", flush=True)
        portfolios[(profile, "control")], logs[(profile, "control")] = solve(
            close, volume, common, profile=profile, score=None
        )
        for name in ("quality", "accruals"):
            print(f"Running {profile}: {name} filter...", flush=True)
            portfolios[(profile, name)], logs[(profile, name)] = solve(
                close, volume, common, profile=profile, score=scores[name]
            )

    windows = {
        "early_2020_2022": slice(None, "2022-12-31"),
        "heldout_2023_plus": slice("2023-01-01", None),
        "full": slice(None),
    }
    performance: dict[str, list[dict]] = {}
    for window, slicer in windows.items():
        performance[window] = []
        for (profile, name), series in portfolios.items():
            sliced = series.loc[slicer].dropna()
            if sliced.empty:
                continue
            performance[window].append(returns_summary(sliced, f"{profile} — {name}"))

    gates = {}
    coverage_by_variant = {}
    for name in ("quality", "accruals"):
        cells = []
        for window in ("early_2020_2022", "heldout_2023_plus"):
            for profile in ("base", "2x"):
                control_row = next(
                    (r for r in performance[window] if r["portfolio"] == f"{profile} — control"), None
                )
                candidate_row = next(
                    (r for r in performance[window] if r["portfolio"] == f"{profile} — {name}"), None
                )
                if control_row and candidate_row:
                    cells.append((window, profile, control_row, candidate_row))
        gates[name] = passes_gate_all_cells(cells, "return_enhancer")

        avg_coverage = {}
        for profile in ("base", "2x"):
            log = logs[(profile, name)]
            avg_coverage[profile] = round(float(log["coverage_pct"].mean()), 4) if len(log) else 0.0
        coverage_by_variant[name] = avg_coverage

    # The feasibility contract's own hypothesis frames this as a risk
    # reducer ("may reduce momentum crashes and distress exposure without
    # replacing the momentum rank"), not a strict return enhancer. Bounds
    # fixed before running: up to 1.5 CAGR points given up is acceptable for
    # at least a 5% relative drawdown improvement - a smaller budget than
    # account_mandate_study.json's 3.0/10%, since this filter is a milder
    # adjustment than a dedicated de-risking mandate.
    risk_reducer_gates = {}
    for name in ("quality", "accruals"):
        risk_reducer_gates[name] = {}
        for window in ("early_2020_2022", "heldout_2023_plus"):
            for profile in ("base", "2x"):
                control_row = next(
                    (r for r in performance[window] if r["portfolio"] == f"{profile} — control"), None
                )
                candidate_row = next(
                    (r for r in performance[window] if r["portfolio"] == f"{profile} — {name}"), None
                )
                if not (control_row and candidate_row):
                    continue
                risk_reducer_gates[name][f"{window}/{profile}"] = passes_gate(
                    control_row, candidate_row, "risk_reducer",
                    max_cagr_cost_pp=1.5, min_dd_improvement_pct=0.05,
                ).to_dict()
    payload = {
        "pre_registration": {
            "candidates": {
                "quality": "equal-weight cross-sectional rank of gross-profit/assets and OCF/assets",
                "accruals": "-(NetIncomeLoss - OperatingCashFlow) / Assets (Sloan 1996)",
            },
            "filter": (
                "Remove bottom-quintile-score names from long candidates and "
                "top-quintile-score names from short candidates, then continue "
                "down the unchanged momentum rank to fill 20 names per leg."
            ),
            "promotion_rule": (
                "return_enhancer (backtest.promotion): higher Sharpe, CAGR not "
                "lower, max drawdown not worse, in both profiles and both "
                "windows. Per reports/quality_momentum_filter_feasibility.json, "
                "coverage must also remain >=80% of otherwise-eligible momentum "
                "candidates."
            ),
            "coverage_requirement_pct": 80.0,
        },
        "reproduction_check": {
            "max_abs_equity_diff_vs_build_portfolio": reproduction_max_abs_diff,
            "passes_agents_md_discipline": reproduction_max_abs_diff < 1e-6,
        },
        "cik_map_coverage": coverage,
        "coverage_by_variant": coverage_by_variant,
        "decision": {},
        "gates": {name: gate for name, gate in gates.items()},
        "risk_reducer_gates": risk_reducer_gates,
        "performance": performance,
        "limitations": [
            "Inherits engine.fundamentals.cik_map()'s current-listing-only "
            "survivorship bias (see cik_map_coverage above) on top of the "
            "price panel's own current-listing bias.",
            "The cross-sectional panel has no data before 2020-07-27 (see "
            "AGENTS.md); combined with fundamentals starting 2019, coverage "
            "in the earliest rebalances may be thinner than the full-sample "
            "average reported in coverage_by_variant.",
            "Whole-share short capacity is not modeled here (idealized "
            "fractional shorts, matching production_portfolio.py's "
            "convention) — see AGENTS.md's short-borrow-convention note.",
            "Historical easy-to-borrow availability is unavailable.",
        ],
    }
    for name in ("quality", "accruals"):
        coverage_ok = all(v >= 0.80 for v in coverage_by_variant[name].values())
        gate_ok = gates[name]["passed"]
        if not coverage_ok:
            payload["decision"][name] = "defer_insufficient_coverage"
        else:
            payload["decision"][name] = "promote" if gate_ok else "reject"

    out = Path("reports/fundamental_momentum_filter_study.json")
    out.write_text(json.dumps(payload, indent=2, default=str))
    print(f"\nReproduction check max abs diff: {reproduction_max_abs_diff:.2e}")
    print("Coverage by variant:", json.dumps(coverage_by_variant, indent=2))
    print("Decisions:", json.dumps(payload["decision"], indent=2))
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
