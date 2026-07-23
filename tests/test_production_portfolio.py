import pandas as pd

from backtest.clone_study import build_weights
from backtest.production_portfolio import norm_index, returns_summary
from engine.config import load_config
from engine.portfolio import equity_core_targets


def test_norm_index_aligns_different_intraday_utc_stamps():
    s = pd.Series(
        [1.0, 2.0],
        index=pd.to_datetime(["2026-01-02T05:00:00Z", "2026-01-05T04:00:00Z"]),
    )
    out = norm_index(s)
    assert list(out.index) == list(pd.to_datetime(["2026-01-02", "2026-01-05"]))


def test_return_summary_reports_compounding_and_drawdown():
    r = pd.Series(
        [0.10, -0.20, 0.10],
        index=pd.to_datetime(["2024-01-02", "2024-07-01", "2025-01-02"]),
    )
    result = returns_summary(r, "test")
    assert result["portfolio"] == "test"
    assert result["x_money"] == 0.968
    assert result["max_dd"] == -0.2


def test_clone_weights_never_appear_before_timezone_naive_filing_event():
    holdings = pd.DataFrame({
        "cik": [1],
        "period": pd.to_datetime(["2021-03-31"]),
        "filing_date": pd.to_datetime(["2021-05-15"]),
        "symbol": ["XLK"],
        "value": [100.0],
    })
    dates = pd.date_range("2021-01-01", "2021-06-01", tz="UTC")
    weights = build_weights(holdings, dates, top_n=1)
    assert weights.loc[:"2021-05-15", "XLK"].sum() == 0
    assert weights.loc["2021-05-16":, "XLK"].eq(1).all()


def test_equity_core_matches_configured_spy_allocation():
    cfg = load_config()
    assert equity_core_targets(cfg) == {
        "SPY": cfg.sleeves_paper["sleeves"]["equity_core"]
    }
