import pandas as pd
import pytest

from backtest.bull_call_spread_study import (
    black_scholes_call,
    bullish_signal,
    modeled_call_spread,
    select_call_spread,
)


def test_select_call_spread_uses_standard_expiry_and_ordered_strikes():
    contracts = []
    for expiry in ("2026-03-13", "2026-03-20"):
        for strike in (100, 105, 110, 115):
            contracts.append({
                "expiration_date": expiry,
                "strike_price": str(strike),
                "symbol": f"C{expiry}{strike}",
            })
    plan = select_call_spread(
        contracts,
        roll_date=pd.Timestamp("2026-01-20"),
        spot_reference=100,
    )
    assert plan.expiration_date == "2026-03-20"
    assert plan.long_strike == 105
    assert plan.short_strike == 110


def test_call_value_converges_to_intrinsic_at_expiry():
    assert black_scholes_call(120, 100, 0, 0.20) == 20
    assert black_scholes_call(80, 100, 0, 0.20) == 0


def test_modeled_call_spread_is_bounded_by_width():
    value = modeled_call_spread(
        100,
        long_strike=105,
        short_strike=110,
        days_to_expiry=60,
        vix=18,
    )
    assert 0 < value < 5


def test_bullish_signal_does_not_see_current_session():
    index = pd.bdate_range("2025-01-01", periods=240)
    original = pd.Series(
        [100 + 0.05 * number for number in range(240)],
        index=index,
    )
    changed = original.copy()
    changed.iloc[-1] = 1
    assert bullish_signal(original, index[-1]) == bullish_signal(
        changed, index[-1]
    )


def test_call_spread_rejects_empty_chain():
    with pytest.raises(ValueError, match="no call contracts"):
        select_call_spread(
            [],
            roll_date=pd.Timestamp("2026-01-20"),
            spot_reference=100,
        )
