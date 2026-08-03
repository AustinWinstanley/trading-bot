import pytest

from backtest.promotion import passes_gate, passes_gate_all_cells


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
