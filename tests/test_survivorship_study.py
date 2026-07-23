import pandas as pd

from backtest.survivorship_study import terminal_overrides


def test_terminal_overrides_land_on_next_session_and_detect_distress():
    index = pd.date_range("2026-01-01", periods=5, freq="B")
    close = pd.DataFrame(
        {
            "FAILED": [100.0, 60.0, 10.0, None, None],
            "ACQUIRED": [100.0, 101.0, 102.0, None, None],
        },
        index=index,
    )
    overrides = terminal_overrides(
        close, ["FAILED", "ACQUIRED"], mode="distress_to_zero"
    )
    assert overrides.at[index[3], "FAILED"] == -1.0
    assert pd.isna(overrides.at[index[3], "ACQUIRED"])


def test_universal_haircut_applies_to_every_ended_symbol():
    index = pd.date_range("2026-01-01", periods=4, freq="B")
    close = pd.DataFrame({"X": [10.0, 11.0, None, None]}, index=index)
    overrides = terminal_overrides(close, ["X"], mode="all_minus_30pct")
    assert overrides.at[index[2], "X"] == -0.30
