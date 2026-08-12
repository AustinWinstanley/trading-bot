import pytest

from backtest.bull_put_credit_spread_study import (
    credit_spread_terms,
    diagnose_budget_feasibility,
)


def test_credit_spread_profit_and_loss_include_round_trip_friction():
    profit, loss = credit_spread_terms(5.0, 1.25, 0.10)
    assert profit == pytest.approx(85.0)
    assert loss == pytest.approx(415.0)
    assert profit + loss == pytest.approx(500.0)


@pytest.mark.parametrize("credit", [0.0, -1.0, 5.0, 6.0])
def test_credit_spread_rejects_invalid_credit(credit):
    with pytest.raises(ValueError):
        credit_spread_terms(5.0, credit, 0.10)


def test_diagnose_budget_feasibility_flags_when_every_candidate_fails_budget():
    # Mirrors what actually happened in reports/bull_put_fixed_width_study.json:
    # 0 completed spreads, every candidate that reached the check rejected
    # for budget specifically — a design bug (width vs. observed credit),
    # not a market-opportunity finding, and the diagnostic must say so.
    logs = [
        {"maximum_loss_dollars": 520.0, "entry_credit": 0.15,
         "rejected": "maximum loss exceeds 5% budget"},
        {"maximum_loss_dollars": 535.0, "entry_credit": 0.05,
         "rejected": "maximum loss exceeds 5% budget"},
        {"rejected": "missing entry bar"},  # never reached the budget check
    ]
    result = diagnose_budget_feasibility(
        logs, budget_rejection_reason="maximum loss exceeds 5% budget"
    )
    assert result["candidates_reaching_budget_check"] == 2
    assert result["rejected_for_budget"] == 2
    assert result["structurally_infeasible"] is True
    assert result["observed_entry_credit_min"] == 0.05
    assert result["observed_entry_credit_max"] == 0.15


def test_diagnose_budget_feasibility_not_infeasible_when_some_pass():
    logs = [
        {"maximum_loss_dollars": 480.0, "entry_credit": 0.45},  # passed
        {"maximum_loss_dollars": 535.0, "entry_credit": 0.05,
         "rejected": "maximum loss exceeds 5% budget"},
    ]
    result = diagnose_budget_feasibility(
        logs, budget_rejection_reason="maximum loss exceeds 5% budget"
    )
    assert result["structurally_infeasible"] is False


def test_diagnose_budget_feasibility_handles_no_candidates_reaching_check():
    logs = [{"rejected": "missing entry bar"}, {"rejected": "invalid entry credit"}]
    result = diagnose_budget_feasibility(
        logs, budget_rejection_reason="maximum loss exceeds 5% budget"
    )
    assert result["candidates_reaching_budget_check"] == 0
    assert result["structurally_infeasible"] is False
    assert result["observed_entry_credit_min"] is None
