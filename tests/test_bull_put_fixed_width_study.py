import pandas as pd

from backtest.bull_put_fixed_width_study import select_fixed_width_put_spread


def test_fixed_width_selection_uses_exact_lower_strike():
    contracts = [
        {
            "symbol": f"SPY261218P00{strike}000",
            "expiration_date": "2026-12-18",
            "strike_price": str(strike),
        }
        for strike in (540, 545, 550, 555)
    ]
    plan = select_fixed_width_put_spread(
        contracts,
        roll_date=pd.Timestamp("2026-11-02"),
        spot_reference=610.0,
    )
    assert plan.long_strike == 550
    assert plan.short_strike == 545
    assert plan.long_strike - plan.short_strike == 5
