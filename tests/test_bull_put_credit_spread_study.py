import pytest

from backtest.bull_put_credit_spread_study import credit_spread_terms


def test_credit_spread_profit_and_loss_include_round_trip_friction():
    profit, loss = credit_spread_terms(5.0, 1.25, 0.10)
    assert profit == pytest.approx(85.0)
    assert loss == pytest.approx(415.0)
    assert profit + loss == pytest.approx(500.0)


@pytest.mark.parametrize("credit", [0.0, -1.0, 5.0, 6.0])
def test_credit_spread_rejects_invalid_credit(credit):
    with pytest.raises(ValueError):
        credit_spread_terms(5.0, credit, 0.10)
