import pandas as pd
import pytest

from backtest.momentum_sleeve_overlay_study import weekly_de_risk_scale


def test_scale_uses_only_prior_returns_and_never_increases_exposure():
    index = pd.bdate_range("2025-01-01", periods=80)
    calm = pd.Series([0.001, -0.001] * 40, index=index)
    shocked = calm.copy()
    shocked.iloc[-1] = 0.20
    before = weekly_de_risk_scale(calm, 0.15)
    after = weekly_de_risk_scale(shocked, 0.15)
    pd.testing.assert_series_equal(before, after)
    assert before.between(0.25, 1.0).all()


def test_scale_rejects_invalid_parameters():
    returns = pd.Series([0.0] * 80)
    with pytest.raises(ValueError, match="target_vol"):
        weekly_de_risk_scale(returns, 0)
    with pytest.raises(ValueError, match="min_scale"):
        weekly_de_risk_scale(returns, 0.15, min_scale=0)
