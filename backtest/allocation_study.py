"""Split-sample allocation study for the production return streams."""

from __future__ import annotations

import json
from pathlib import Path

from backtest.production_portfolio import (
    SHORT_BORROW,
    TD,
    build_streams,
    returns_summary,
)

VARIANTS = {
    "deployed clone mix": {"clone": 0.40, "tsmom": 0.25, "trend": 0.20, "mom_ls": 0.30},
    "SPY equity core": {"spy": 0.40, "tsmom": 0.25, "trend": 0.20, "mom_ls": 0.30},
    "barbell 55/25/20": {"spy": 0.55, "tsmom": 0.25, "mom_ls": 0.40},
    "barbell 45/35/20": {"spy": 0.45, "tsmom": 0.35, "mom_ls": 0.40},
    "higher MOM_LS": {"spy": 0.40, "tsmom": 0.20, "mom_ls": 0.60},
}


def evaluate_variant(streams, weights, label):
    r = sum(weight * streams[name] for name, weight in weights.items())
    short_weight = weights.get("mom_ls", 0.0) / 2
    return returns_summary(r - short_weight * SHORT_BORROW / TD, label)


def main() -> None:
    streams = build_streams()
    windows = {
        "full": streams,
        "early_2020_2022": streams.loc[:"2022-12-31"],
        "heldout_2023_2026": streams.loc["2023-01-01":],
    }
    results = {
        window: [
            evaluate_variant(frame, weights, name)
            for name, weights in VARIANTS.items()
        ]
        for window, frame in windows.items()
    }
    out = Path("reports/allocation_study.json")
    out.write_text(json.dumps({
        "note": (
            "Currently-listed universe; positive estimates are survivorship-biased. "
            "No optimizer: five fixed, interpretable allocations compared in early "
            "and held-out windows."
        ),
        "weights": VARIANTS,
        "results": results,
    }, indent=2))
    for window, rows in results.items():
        print(f"\n{window}")
        for row in rows:
            print(f"  {row['portfolio']:<22} CAGR {row['cagr']:>7.2%} "
                  f"Sharpe {row['sharpe']:>5.3f} DD {row['max_dd']:>7.2%}")
    print(f"\nWrote {out}")


if __name__ == "__main__":
    main()
