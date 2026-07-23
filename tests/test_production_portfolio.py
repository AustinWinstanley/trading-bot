import pandas as pd
import pytest

from backtest.production_portfolio import require_history


def test_research_cache_fails_closed_when_configured_history_is_missing():
    with pytest.raises(FileNotFoundError, match=r"GLD.*TLT"):
        require_history(
            ["GLD", "SPY", "TLT"],
            {"SPY": pd.DataFrame()},
            label="TSMOM",
        )
