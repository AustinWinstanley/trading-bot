import pandas as pd
import pytest

from backtest.spy_put_spread_study import (
    add_dollar_pnl,
    budget_diagnostics,
    select_put_spread,
)


def test_contract_selection_uses_nearest_expiry_and_moneyness():
    contracts = []
    for expiry in ("2026-02-13", "2026-02-20"):
        for strike in (85, 90, 95, 100):
            contracts.append({
                "expiration_date": expiry,
                "strike_price": str(strike),
                "symbol": f"P{expiry}{strike}",
            })
    plan = select_put_spread(
        contracts,
        roll_date=pd.Timestamp("2026-01-05"),
        spot_reference=100,
    )
    assert plan.expiration_date == "2026-02-20"
    assert plan.long_strike == 95
    assert plan.short_strike == 90


def test_dollar_option_pnl_is_scaled_by_prior_equity():
    index = pd.bdate_range("2026-01-01", periods=2)
    returns = pd.Series([0.0, 0.10], index=index)
    pnl = pd.Series([100.0, -50.0], index=index)
    result = add_dollar_pnl(returns, pnl, starting_equity=10_000)
    assert result.iloc[0] == pytest.approx(0.01)
    assert result.iloc[1] == pytest.approx(0.10 - 50 / 10_100)


def test_contract_selection_rejects_empty_chain():
    with pytest.raises(ValueError, match="no put contracts"):
        select_put_spread(
            [],
            roll_date=pd.Timestamp("2026-01-05"),
            spot_reference=100,
        )


def test_budget_diagnostics_counts_contracts_that_fit_account_limits():
    logs = [
        {"enabled": True, "maximum_loss_dollars": 90},
        {"enabled": True, "maximum_loss_dollars": 190},
        {"enabled": True, "maximum_loss_dollars": 310},
        {"enabled": False, "maximum_loss_dollars": 10},
        {"enabled": True, "rejected": "missing entry bar"},
    ]
    result = budget_diagnostics(logs, account_equity=10_000)
    assert result["completed_spreads"] == 3
    assert result["median_maximum_loss_pct"] == pytest.approx(0.019)
    assert result["maximum_maximum_loss_pct"] == pytest.approx(0.031)
    assert result["trades_within_budget"] == {
        "1%": 1,
        "2%": 2,
        "3%": 2,
    }
