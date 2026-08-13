import numpy as np
import pytest

from backtest.promotion import (
    paired_drawdown_noise_pp,
    passes_gate,
    passes_gate_all_cells,
)


def summary(sharpe, cagr, max_dd):
    return {"sharpe": sharpe, "cagr": cagr, "max_dd": max_dd}


class TestReturnEnhancer:
    def test_passes_when_strictly_better_on_all_three(self):
        control = summary(1.0, 0.10, -0.20)
        candidate = summary(1.1, 0.11, -0.18)
        result = passes_gate(control, candidate, "return_enhancer")
        assert result.passed
        assert result.checks == {
            "sharpe_higher": True,
            "cagr_not_lower": True,
            "max_dd_not_worse": True,
        }

    def test_fails_on_equal_sharpe(self):
        # AGENTS.md says "higher Sharpe" — equal does not qualify.
        control = summary(1.0, 0.10, -0.20)
        candidate = summary(1.0, 0.12, -0.18)
        result = passes_gate(control, candidate, "return_enhancer")
        assert not result.passed
        assert result.checks["sharpe_higher"] is False

    def test_fails_on_worse_drawdown(self):
        control = summary(1.0, 0.10, -0.20)
        candidate = summary(1.1, 0.11, -0.21)
        result = passes_gate(control, candidate, "return_enhancer")
        assert not result.passed
        assert result.checks["max_dd_not_worse"] is False

    def test_missing_key_raises(self):
        with pytest.raises(KeyError):
            passes_gate({"sharpe": 1.0}, summary(1.0, 0.1, -0.1))


class TestRiskReducer:
    def test_passes_within_cagr_budget_and_dd_target(self):
        control = summary(1.0, 0.10, -0.30)
        candidate = summary(0.95, 0.085, -0.20)  # -1.5pp CAGR, DD improves 33%
        result = passes_gate(
            control,
            candidate,
            "risk_reducer",
            max_cagr_cost_pp=2.0,
            min_dd_improvement_pct=0.25,
        )
        assert result.passed
        assert result.inputs["cagr_cost_pp"] == pytest.approx(1.5)
        assert result.inputs["dd_improvement_pct"] == pytest.approx(1 / 3, rel=1e-3)

    def test_fails_when_cagr_cost_exceeds_budget(self):
        control = summary(1.0, 0.10, -0.30)
        candidate = summary(0.95, 0.07, -0.15)  # -3pp CAGR, but budget is 2pp
        result = passes_gate(
            control,
            candidate,
            "risk_reducer",
            max_cagr_cost_pp=2.0,
            min_dd_improvement_pct=0.25,
        )
        assert not result.passed
        assert result.checks["cagr_cost_within_budget"] is False

    def test_fails_when_drawdown_barely_improves(self):
        control = summary(1.0, 0.10, -0.30)
        candidate = summary(0.95, 0.09, -0.29)  # DD improvement ~3%, target 25%
        result = passes_gate(
            control,
            candidate,
            "risk_reducer",
            max_cagr_cost_pp=2.0,
            min_dd_improvement_pct=0.25,
        )
        assert not result.passed
        assert result.checks["drawdown_improves_enough"] is False

    def test_requires_prereg_bounds(self):
        with pytest.raises(ValueError):
            passes_gate(summary(1, 0.1, -0.1), summary(1, 0.1, -0.1), "risk_reducer")


class TestCostReducer:
    def test_passes_within_sharpe_budget_and_turnover_target(self):
        control = summary(1.00, 0.10, -0.20)
        candidate = summary(0.98, 0.10, -0.20)  # -0.02 Sharpe, within 0.05 budget
        result = passes_gate(
            control,
            candidate,
            "cost_reducer",
            min_turnover_reduction_pct=0.30,
            max_sharpe_cost=0.05,
            control_turnover=20.0,
            candidate_turnover=12.0,  # 40% cut
        )
        assert result.passed
        assert result.inputs["turnover_reduction_pct"] == pytest.approx(0.40)

    def test_fails_when_turnover_cut_is_too_small(self):
        control = summary(1.00, 0.10, -0.20)
        candidate = summary(0.98, 0.10, -0.20)
        result = passes_gate(
            control,
            candidate,
            "cost_reducer",
            min_turnover_reduction_pct=0.30,
            max_sharpe_cost=0.05,
            control_turnover=20.0,
            candidate_turnover=16.0,  # only 20% cut
        )
        assert not result.passed
        assert result.checks["turnover_reduced_enough"] is False

    def test_requires_prereg_bounds(self):
        with pytest.raises(ValueError):
            passes_gate(
                summary(1, 0.1, -0.1),
                summary(1, 0.1, -0.1),
                "cost_reducer",
                min_turnover_reduction_pct=0.3,
            )


class TestAllCells:
    def test_passes_only_when_every_cell_passes(self):
        good = summary(1.1, 0.11, -0.18)
        bad = summary(0.9, 0.09, -0.22)
        control = summary(1.0, 0.10, -0.20)
        cells = [
            ("early_2020_2022", "base", control, good),
            ("early_2020_2022", "2x", control, good),
            ("heldout_2023_plus", "base", control, good),
            ("heldout_2023_plus", "2x", control, bad),
        ]
        result = passes_gate_all_cells(cells, "return_enhancer")
        assert not result["passed"]
        passed_flags = [c["passed"] for c in result["cells"]]
        assert passed_flags == [True, True, True, False]

    def test_empty_cells_does_not_pass(self):
        result = passes_gate_all_cells([], "return_enhancer")
        assert not result["passed"]

    def test_unknown_objective_class_raises(self):
        control = summary(1.0, 0.10, -0.20)
        candidate = summary(1.1, 0.11, -0.18)
        with pytest.raises(ValueError):
            passes_gate(control, candidate, "not_a_real_class")


class TestNoEffect:
    """A byte-identical control/candidate (zero historical events matched a
    filter, zero trades cleared a feasibility budget, ...) must not read the
    same as a genuine loss — see reports/tsmom_liquidity_alignment_study.json
    and reports/bull_put_fixed_width_study.json, both of which recorded
    passed: false from four cells with d_sharpe == d_cagr == d_max_dd == 0.0."""

    def test_identical_control_and_candidate_is_no_effect_not_failed(self):
        control = summary(0.927, 0.1006, -0.0994)
        candidate = summary(0.927, 0.1006, -0.0994)
        result = passes_gate(control, candidate, "return_enhancer")
        assert result.no_effect is True
        assert result.verdict == "no_effect"
        # passed stays exactly as computed (still False, for anything that
        # only reads the old field) — no_effect/verdict are additive.
        assert result.passed is False

    def test_genuine_loss_is_not_flagged_as_no_effect(self):
        # Same case as test_fails_on_equal_sharpe above: only sharpe ties,
        # cagr and drawdown genuinely differ — this is a real comparison,
        # not an untested candidate, and must not be relabeled no_effect.
        control = summary(1.0, 0.10, -0.20)
        candidate = summary(1.0, 0.12, -0.18)
        result = passes_gate(control, candidate, "return_enhancer")
        assert result.no_effect is False
        assert result.verdict == "failed"

    def test_genuine_pass_has_passed_verdict(self):
        control = summary(1.0, 0.10, -0.20)
        candidate = summary(1.1, 0.11, -0.18)
        result = passes_gate(control, candidate, "return_enhancer")
        assert result.no_effect is False
        assert result.verdict == "passed"

    def test_all_cells_surfaces_no_effect_at_the_aggregate_level(self):
        identical = summary(0.927, 0.1006, -0.0994)
        cells = [
            ("early_2020_2022", "base", identical, identical),
            ("early_2020_2022", "2x", identical, identical),
            ("heldout_2023_plus", "base", identical, identical),
            ("heldout_2023_plus", "2x", identical, identical),
        ]
        result = passes_gate_all_cells(cells, "return_enhancer")
        assert result["passed"] is False
        assert result["any_no_effect"] is True
        assert result["all_no_effect"] is True

    def test_all_cells_mixed_no_effect_and_real_loss(self):
        identical = summary(1.0, 0.10, -0.20)
        loser = summary(0.9, 0.09, -0.22)
        cells = [
            ("early_2020_2022", "base", identical, identical),
            ("early_2020_2022", "2x", identical, loser),
        ]
        result = passes_gate_all_cells(cells, "return_enhancer")
        assert result["any_no_effect"] is True
        assert result["all_no_effect"] is False


class TestDiversifier:
    CONTROL = {"sharpe": 1.0, "cagr": 0.10, "max_dd": -0.15}

    def kwargs(self, **overrides):
        base = {"max_dd_cost_pp": 0.8, "max_correlation": 0.40, "stream_correlation": 0.28}
        base.update(overrides)
        return base

    def test_requires_all_three_declared_parameters(self):
        candidate = summary(1.05, 0.11, -0.15)
        for missing in ("max_dd_cost_pp", "max_correlation", "stream_correlation"):
            kwargs = self.kwargs()
            kwargs.pop(missing)
            with pytest.raises(ValueError, match="diversifier requires"):
                passes_gate(self.CONTROL, candidate, "diversifier", **kwargs)

    def test_passes_with_dd_cost_inside_the_noise_band(self):
        # 0.5pp worse drawdown, inside the 0.8pp declared band -> a tie.
        candidate = summary(1.05, 0.11, -0.155)
        result = passes_gate(self.CONTROL, candidate, "diversifier", **self.kwargs())
        assert result.passed is True
        assert result.checks["max_dd_within_noise"] is True

    def test_fails_when_dd_cost_exceeds_the_band(self):
        # 1.0pp worse drawdown against a 0.8pp band -> a real loss.
        candidate = summary(1.05, 0.11, -0.16)
        result = passes_gate(self.CONTROL, candidate, "diversifier", **self.kwargs())
        assert result.passed is False
        assert result.checks["max_dd_within_noise"] is False

    def test_sharpe_and_cagr_remain_strict(self):
        flat_sharpe = summary(1.0, 0.11, -0.15)
        assert passes_gate(self.CONTROL, flat_sharpe, "diversifier", **self.kwargs()).passed is False
        lower_cagr = summary(1.05, 0.09, -0.15)
        assert passes_gate(self.CONTROL, lower_cagr, "diversifier", **self.kwargs()).passed is False

    def test_high_correlation_fails(self):
        candidate = summary(1.05, 0.11, -0.15)
        result = passes_gate(
            self.CONTROL, candidate, "diversifier",
            **self.kwargs(stream_correlation=0.55),
        )
        assert result.passed is False
        assert result.checks["correlation_low_enough"] is False

    def test_inputs_echo_the_declared_parameters(self):
        candidate = summary(1.05, 0.11, -0.15)
        result = passes_gate(self.CONTROL, candidate, "diversifier", **self.kwargs())
        assert result.inputs["max_dd_cost_pp"] == 0.8
        assert result.inputs["max_correlation"] == 0.40
        assert result.inputs["stream_correlation"] == 0.28


class TestPairedDrawdownNoise:
    def test_identical_streams_have_zero_band(self):
        rng = np.random.default_rng(1)
        returns = rng.normal(0.0005, 0.01, 400)
        assert paired_drawdown_noise_pp(returns, returns.copy()) == 0.0

    def test_deterministic_for_a_fixed_seed(self):
        rng = np.random.default_rng(2)
        a = rng.normal(0.0005, 0.01, 400)
        b = rng.normal(0.0005, 0.012, 400)
        assert paired_drawdown_noise_pp(a, b) == paired_drawdown_noise_pp(a, b)

    def test_nearly_identical_streams_have_a_small_band(self):
        """The realistic case: control vs control + a tiny sleeve. The band
        should be far smaller than for independent streams."""
        rng = np.random.default_rng(3)
        control = rng.normal(0.0005, 0.01, 600)
        tiny_sleeve = rng.normal(0.0004, 0.02, 600)
        candidate = 0.95 * control + 0.05 * tiny_sleeve
        near_band = paired_drawdown_noise_pp(control, candidate)
        independent = rng.normal(0.0005, 0.01, 600)
        far_band = paired_drawdown_noise_pp(control, independent)
        assert 0 < near_band < far_band

    def test_rejects_mismatched_lengths(self):
        with pytest.raises(ValueError, match="equal-length"):
            paired_drawdown_noise_pp(np.zeros(100), np.zeros(101))

    def test_rejects_nans(self):
        a = np.zeros(100)
        b = np.zeros(100)
        b[5] = np.nan
        with pytest.raises(ValueError, match="NaN"):
            paired_drawdown_noise_pp(a, b)

    def test_rejects_too_short_series(self):
        with pytest.raises(ValueError, match="block_size"):
            paired_drawdown_noise_pp(np.zeros(10), np.zeros(10), block_size=63)
