import pandas as pd
import pytest

from backtest.production_portfolio import require_history, trend_stream


def test_research_cache_fails_closed_when_configured_history_is_missing():
    with pytest.raises(FileNotFoundError, match=r"GLD.*TLT"):
        require_history(
            ["GLD", "SPY", "TLT"],
            {"SPY": pd.DataFrame()},
            label="TSMOM",
        )


def test_trend_warmup_is_computed_before_sample_trim():
    index = pd.bdate_range("2024-01-01", periods=260)
    spy = pd.Series(range(100, 360), index=index, dtype=float)
    sample = trend_stream(spy).reindex(index[-10:])
    assert sample.notna().all()
    assert (sample > 0).all()
