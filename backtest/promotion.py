"""Shared promotion-gate logic for backtest studies.

Every study that decides whether a candidate replaces or augments a control
should call `passes_gate` / `passes_gate_all_cells` here rather than
hand-rolling its own comparison. Before this module existed, seven studies
each implemented a different rule — some strict `>`, some `>=`, some with a
CAGR-retention or drawdown-tolerance band baked in ad hoc — so "passed the
promotion gate" did not mean the same thing across `reports/`. See
AGENTS.md's "Research conventions" for the standing bar this codifies.

Objective classes
------------------
Not every candidate is trying to do the same thing, and judging a tail hedge
on "does it raise CAGR" or a defensive mandate on strict Sharpe improvement
manufactures rejections that have nothing to do with whether the candidate
did its job. A study picks one class *before* looking at results — putting
the bound in the pre-registration, not in the writeup after the run:

- `return_enhancer` — the default. Higher Sharpe, CAGR no lower, drawdown no
  worse. This is the AGENTS.md bar for anything claiming to improve
  risk-adjusted return outright.
- `risk_reducer` — trading a bounded amount of CAGR for a drawdown/tail
  improvement (a hedge, a vol overlay, a defensive mandate). The study must
  pre-declare `max_cagr_cost_pp` (CAGR points it is willing to give up) and
  `min_dd_improvement_pct` (relative drawdown reduction it is trying to buy
  with that cost). Sharpe is reported but not gating.
- `cost_reducer` — trading turnover for a small, explicitly-bounded Sharpe
  give-up (a rank buffer, a slower rebalance). The study must pre-declare
  `min_turnover_reduction_pct` and `max_sharpe_cost` (absolute Sharpe it is
  willing to give up), and pass the two turnover figures being compared.

Every input that decided the verdict is echoed back in the result so the
report JSON is self-auditing — no re-deriving what a study "must have" used.
"""

from __future__ import annotations

from dataclasses import dataclass

OBJECTIVE_CLASSES = ("return_enhancer", "risk_reducer", "cost_reducer")

# Below this, d_sharpe/d_cagr/d_max_dd are treated as "the candidate produced
# no measurable difference from the control" rather than as a genuine, if
# small, loss. This is not about statistical noise (there is no significance
# test here) — it catches the specific failure mode of a control and
# candidate that are numerically IDENTICAL: zero historical events matched a
# filter, zero trades cleared a feasibility constraint, or any other path
# where the candidate's logic never actually diverged from the control's.
# `sharpe_higher: d_sharpe > 0` reads that exactly the same as a real loss —
# `passed: False` either way — even though "untested" and "tested and lost"
# are different findings that call for different next steps.
NO_EFFECT_EPSILON = 1e-9


@dataclass(frozen=True)
class GateResult:
    objective_class: str
    passed: bool
    checks: dict
    inputs: dict
    no_effect: bool = False

    @property
    def verdict(self) -> str:
        """One of "no_effect", "passed", "failed" — the field a report
        reader should actually branch on. `passed` stays exactly as
        computed for backward compatibility with any code/report already
        reading it; `verdict` is the additive, unambiguous signal."""
        if self.no_effect:
            return "no_effect"
        return "passed" if self.passed else "failed"

    def to_dict(self) -> dict:
        return {
            "objective_class": self.objective_class,
            "passed": self.passed,
            "no_effect": self.no_effect,
            "verdict": self.verdict,
            "checks": self.checks,
            "inputs": self.inputs,
        }


def _require(summary: dict, *keys: str) -> None:
    missing = [k for k in keys if k not in summary]
    if missing:
        raise KeyError(f"returns_summary is missing required keys: {missing}")


def passes_gate(
    control: dict,
    candidate: dict,
    objective_class: str = "return_enhancer",
    *,
    max_cagr_cost_pp: float | None = None,
    min_dd_improvement_pct: float | None = None,
    min_turnover_reduction_pct: float | None = None,
    max_sharpe_cost: float | None = None,
    control_turnover: float | None = None,
    candidate_turnover: float | None = None,
) -> GateResult:
    """Evaluate one control/candidate pair from `returns_summary` output.

    `control` and `candidate` are dicts shaped like
    `backtest.production_portfolio.returns_summary` output (must carry
    `sharpe`, `cagr`, `max_dd`; `max_dd` is signed non-positive, so a larger
    magnitude is worse). Returns a `GateResult` whose `checks`/`inputs` are
    meant to be embedded directly in the study's report JSON.
    """
    _require(control, "sharpe", "cagr", "max_dd")
    _require(candidate, "sharpe", "cagr", "max_dd")
    if objective_class not in OBJECTIVE_CLASSES:
        raise ValueError(f"unknown objective_class: {objective_class!r}")

    d_sharpe = candidate["sharpe"] - control["sharpe"]
    d_cagr = candidate["cagr"] - control["cagr"]
    # max_dd is <= 0; d_max_dd > 0 means the candidate's drawdown is shallower.
    d_max_dd = candidate["max_dd"] - control["max_dd"]
    no_effect = (
        abs(d_sharpe) < NO_EFFECT_EPSILON
        and abs(d_cagr) < NO_EFFECT_EPSILON
        and abs(d_max_dd) < NO_EFFECT_EPSILON
    )

    inputs = {
        "control_sharpe": control["sharpe"],
        "candidate_sharpe": candidate["sharpe"],
        "control_cagr": control["cagr"],
        "candidate_cagr": candidate["cagr"],
        "control_max_dd": control["max_dd"],
        "candidate_max_dd": candidate["max_dd"],
        "d_sharpe": round(d_sharpe, 4),
        "d_cagr": round(d_cagr, 4),
        "d_max_dd": round(d_max_dd, 4),
    }

    if objective_class == "return_enhancer":
        checks = {
            "sharpe_higher": d_sharpe > 0,
            "cagr_not_lower": d_cagr >= 0,
            "max_dd_not_worse": d_max_dd >= 0,
        }

    elif objective_class == "risk_reducer":
        if max_cagr_cost_pp is None or min_dd_improvement_pct is None:
            raise ValueError(
                "risk_reducer requires pre-declared max_cagr_cost_pp and "
                "min_dd_improvement_pct"
            )
        cagr_cost_pp = -d_cagr * 100  # positive = CAGR points given up
        dd_improvement_pct = (
            d_max_dd / abs(control["max_dd"]) if control["max_dd"] else 0.0
        )
        checks = {
            "cagr_cost_within_budget": cagr_cost_pp <= max_cagr_cost_pp,
            "drawdown_improves_enough": dd_improvement_pct >= min_dd_improvement_pct,
            "not_worse_on_every_axis": not (
                d_sharpe < 0 and d_cagr < 0 and d_max_dd < 0
            ),
        }
        inputs.update(
            cagr_cost_pp=round(cagr_cost_pp, 3),
            max_cagr_cost_pp=max_cagr_cost_pp,
            dd_improvement_pct=round(dd_improvement_pct, 4),
            min_dd_improvement_pct=min_dd_improvement_pct,
        )

    else:  # cost_reducer
        if (
            min_turnover_reduction_pct is None
            or max_sharpe_cost is None
            or control_turnover is None
            or candidate_turnover is None
        ):
            raise ValueError(
                "cost_reducer requires pre-declared min_turnover_reduction_pct, "
                "max_sharpe_cost, control_turnover, and candidate_turnover"
            )
        turnover_reduction_pct = (
            1 - candidate_turnover / control_turnover if control_turnover else 0.0
        )
        sharpe_cost = -d_sharpe
        checks = {
            "turnover_reduced_enough": (
                turnover_reduction_pct >= min_turnover_reduction_pct
            ),
            "sharpe_cost_within_budget": sharpe_cost <= max_sharpe_cost,
            "cagr_not_lower": d_cagr >= 0,
            "max_dd_not_worse": d_max_dd >= 0,
        }
        inputs.update(
            control_turnover=control_turnover,
            candidate_turnover=candidate_turnover,
            turnover_reduction_pct=round(turnover_reduction_pct, 4),
            min_turnover_reduction_pct=min_turnover_reduction_pct,
            sharpe_cost=round(sharpe_cost, 4),
            max_sharpe_cost=max_sharpe_cost,
        )

    return GateResult(
        objective_class, bool(all(checks.values())), checks, inputs, no_effect
    )


def passes_gate_all_cells(
    cells: list[tuple[str, str, dict, dict]],
    objective_class: str = "return_enhancer",
    **kwargs,
) -> dict:
    """Evaluate (window, profile, control, candidate) cells; require all pass.

    `cells` is `[(window, profile, control_summary, candidate_summary), ...]`
    — normally the four (early/held-out) x (base/2x) combinations. Returns a
    dict with `passed` (True only if every cell passes and at least one cell
    was evaluated) and `cells` (each cell's window/profile tag plus its
    `GateResult`), matching the "both profiles and both windows" convention.

    `any_no_effect` / `all_no_effect` surface the no_effect signal at the
    aggregate level: a `passed: false` result where `all_no_effect` is true
    means every cell was a byte-identical control/candidate — the candidate
    was never actually exercised, not "tested and lost". Check this before
    reading a `passed: false` verdict as a real rejection.
    """
    results = []
    for window, profile, control, candidate in cells:
        result = passes_gate(control, candidate, objective_class, **kwargs)
        results.append({"window": window, "profile": profile, **result.to_dict()})
    return {
        "objective_class": objective_class,
        "passed": bool(results) and all(r["passed"] for r in results),
        "any_no_effect": any(r["no_effect"] for r in results),
        "all_no_effect": bool(results) and all(r["no_effect"] for r in results),
        "cells": results,
    }
