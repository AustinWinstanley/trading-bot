"""Parameter sweep — run many strategy variants and rank them.

    python -m backtest.sweep --preset concentration
    python -m backtest.sweep --preset all --workers 4

Each variant is a set of dotted overrides applied to config.yaml, written to a
temp file and loaded through the normal validated loader — so a variant that
would violate a hard safety rule (averaging down, shorting, market orders)
fails to load rather than silently running.
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import json
import multiprocessing as mp
import tempfile
from pathlib import Path

import pandas as pd
import yaml

from backtest.engine import BacktestConfig, run_backtest
from engine.config import REPO_ROOT, load_config
from engine.tiingo import backtest_universe, load_parquet

BASE_YAML = REPO_ROOT / "config.yaml"


def apply_overrides(raw: dict, overrides: dict) -> dict:
    out = copy.deepcopy(raw)
    for dotted, value in overrides.items():
        node = out
        *parents, leaf = dotted.split(".")
        for key in parents:
            node = node[key]
        node[leaf] = value
    return out


def run_variant(args) -> dict:
    name, overrides, bt_kwargs, start, end = args
    raw = yaml.safe_load(BASE_YAML.read_text())
    mutated = apply_overrides(raw, overrides)

    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
        yaml.safe_dump(mutated, fh)
        path = Path(fh.name)

    try:
        cfg = load_config(path)
        bars = load_parquet(backtest_universe(cfg))
        bt = BacktestConfig(start=start, end=end, **bt_kwargs)
        result = run_backtest(bars, cfg, bt)
        m = result.metrics()
        monthly = result.monthly_returns()
        attr = result.sleeve_attribution()
        return {
            "variant": name,
            "cagr": m["cagr"],
            "sharpe": m["sharpe"],
            "sortino": m["sortino"],
            "max_dd": m["max_drawdown"],
            "vol": m["annual_vol"],
            "final": m["final_equity"],
            "trades": m["trades"],
            "win_rate": m["win_rate"],
            "profit_factor": m["profit_factor"],
            "neg_months": round(float((monthly < 0).mean()), 3) if len(monthly) else None,
            "best_month": round(float(monthly.max()), 4) if len(monthly) else None,
            "worst_month": round(float(monthly.min()), 4) if len(monthly) else None,
            "mo_ge_10pct": int((monthly >= 0.10).sum()) if len(monthly) else 0,
            "halts": sum(1 for n in result.notes if "HALT" in n),
            "attribution": attr.to_dict("records") if not attr.empty else [],
            "overrides": overrides,
        }
    except Exception as exc:  # a variant that cannot load is a result, not a crash
        return {"variant": name, "error": f"{type(exc).__name__}: {exc}", "overrides": overrides}
    finally:
        path.unlink(missing_ok=True)


# --------------------------------------------------------------------------
# Presets
# --------------------------------------------------------------------------


def preset_concentration() -> list[tuple[str, dict, dict]]:
    """Is the account too diversified for its size? Test concentration."""
    return [
        ("baseline", {}, {}),
        ("no-meanrev 60/40",
         {"sleeves.momentum.allocation": 0.60, "sleeves.mean_reversion.allocation": 0.0, "sleeves.pead.allocation": 0.0,
          "sleeves.leveraged.allocation": 0.40},
         {"sleeves": ("momentum", "leveraged")}),
        ("no-meanrev top2",
         {"sleeves.momentum.allocation": 0.60, "sleeves.momentum.hold_top_n": 2,
          "sleeves.mean_reversion.allocation": 0.0, "sleeves.pead.allocation": 0.0, "sleeves.leveraged.allocation": 0.40},
         {"sleeves": ("momentum", "leveraged")}),
        ("no-meanrev top3",
         {"sleeves.momentum.allocation": 0.60, "sleeves.momentum.hold_top_n": 3,
          "sleeves.mean_reversion.allocation": 0.0, "sleeves.pead.allocation": 0.0, "sleeves.leveraged.allocation": 0.40},
         {"sleeves": ("momentum", "leveraged")}),
        ("momentum-only top2",
         {"sleeves.momentum.allocation": 1.0, "sleeves.momentum.hold_top_n": 2,
          "sleeves.mean_reversion.allocation": 0.0, "sleeves.pead.allocation": 0.0, "sleeves.leveraged.allocation": 0.0},
         {"sleeves": ("momentum",)}),
        ("momentum-only top4",
         {"sleeves.momentum.allocation": 1.0, "sleeves.mean_reversion.allocation": 0.0, "sleeves.pead.allocation": 0.0,
          "sleeves.leveraged.allocation": 0.0},
         {"sleeves": ("momentum",)}),
        ("leveraged-only",
         {"sleeves.momentum.allocation": 0.0, "sleeves.mean_reversion.allocation": 0.0, "sleeves.pead.allocation": 0.0,
          "sleeves.leveraged.allocation": 1.0, "risk.max_leveraged_exposure_pct": 1.0,
          "risk.max_position_pct": 0.40},
         {"sleeves": ("leveraged",)}),
        ("aggressive 40/60 top3",
         {"sleeves.momentum.allocation": 0.40, "sleeves.momentum.hold_top_n": 3,
          "sleeves.mean_reversion.allocation": 0.0, "sleeves.pead.allocation": 0.0, "sleeves.leveraged.allocation": 0.60,
          "risk.max_leveraged_exposure_pct": 0.60, "risk.max_position_pct": 0.25},
         {"sleeves": ("momentum", "leveraged")}),
    ]


def preset_risk() -> list[tuple[str, dict, dict]]:
    """How much of the low return is the risk layer choking the strategy?"""
    base = {"sleeves.momentum.allocation": 0.60, "sleeves.momentum.hold_top_n": 3,
            "sleeves.mean_reversion.allocation": 0.0, "sleeves.pead.allocation": 0.0,
            "sleeves.leveraged.allocation": 0.40}
    bt = {"sleeves": ("momentum", "leveraged")}
    return [
        ("core", dict(base), dict(bt)),
        ("wider stops 12%", {**base, "risk.stop_loss_pct": 0.12, "risk.max_stop_distance_pct": 0.20}, dict(bt)),
        ("tighter stops 5%", {**base, "risk.stop_loss_pct": 0.05}, dict(bt)),
        ("no revenge block", {**base, "risk.loss_reentry_block_days": 0}, dict(bt)),
        ("bigger positions 25%", {**base, "risk.max_position_pct": 0.25}, dict(bt)),
        ("deeper halt 30%", {**base, "risk.peak_drawdown_halt_pct": 0.30,
                             "risk.monthly_kill_switch_pct": 0.20}, dict(bt)),
        ("no monthly killswitch", {**base, "risk.monthly_kill_switch_pct": 0.99,
                                   "risk.peak_drawdown_halt_pct": 0.999}, dict(bt)),
    ]


def preset_momentum() -> list[tuple[str, dict, dict]]:
    """Is the momentum lookback itself the problem?"""
    base = {"sleeves.momentum.allocation": 0.60, "sleeves.momentum.hold_top_n": 3,
            "sleeves.mean_reversion.allocation": 0.0, "sleeves.pead.allocation": 0.0,
            "sleeves.leveraged.allocation": 0.40}
    bt = {"sleeves": ("momentum", "leveraged")}
    out = []
    for lb in (3, 6, 9, 12):
        for skip in (0, 1):
            out.append((f"mom {lb}-{skip}mo",
                        {**base, "sleeves.momentum.lookback_months": lb,
                         "sleeves.momentum.skip_months": skip}, dict(bt)))
    return out


PRESETS = {
    "concentration": preset_concentration,
    "risk": preset_risk,
    "momentum": preset_momentum,
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--preset", default="concentration", choices=list(PRESETS) + ["all"])
    ap.add_argument("--start", default="2010-01-04")
    ap.add_argument("--end", default="2026-07-22")
    ap.add_argument("--workers", type=int, default=max(mp.cpu_count() - 1, 1))
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    names = list(PRESETS) if args.preset == "all" else [args.preset]
    variants = []
    for n in names:
        for name, ov, btk in PRESETS[n]():
            variants.append((f"{name}", ov, btk,
                             dt.date.fromisoformat(args.start), dt.date.fromisoformat(args.end)))

    print(f"Running {len(variants)} variants on {args.workers} workers "
          f"({args.start} -> {args.end})...\n")

    with mp.Pool(args.workers) as pool:
        results = pool.map(run_variant, variants)

    ok = [r for r in results if "error" not in r]
    bad = [r for r in results if "error" in r]

    if ok:
        df = pd.DataFrame([{k: v for k, v in r.items()
                            if k not in ("attribution", "overrides")} for r in ok])
        df = df.sort_values("cagr", ascending=False)
        pd.set_option("display.width", 200)
        print(df.to_string(index=False))

        print("\nBest by CAGR:", df.iloc[0]["variant"])
        print("Best by Sharpe:", df.sort_values("sharpe", ascending=False).iloc[0]["variant"])
        best = max(ok, key=lambda r: r["cagr"])
        if best["attribution"]:
            print(f"\nattribution for '{best['variant']}':")
            print(pd.DataFrame(best["attribution"]).to_string(index=False))

    for r in bad:
        print(f"  FAILED {r['variant']}: {r['error']}")

    if args.out:
        Path(args.out).write_text(json.dumps(results, indent=2, default=str))
        print(f"\nWrote {args.out}")


if __name__ == "__main__":
    main()
