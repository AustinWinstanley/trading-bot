import pandas as pd
import pytest

from backtest import windows
from backtest.long_history_stress_study import STRESS_WINDOWS as SOURCE_STRESS


def test_constants_match_the_freeze_policy():
    assert windows.EARLY == ("2020-07-28", "2022-12-30")
    assert windows.HELDOUT == ("2023-01-03", "2026-08-12")
    assert windows.FROZEN_START == "2026-08-13"
    # The held-out screen must stop the day before the frozen window opens.
    assert pd.Timestamp(windows.HELDOUT[1]) < pd.Timestamp(windows.FROZEN_START)
    assert pd.Timestamp(windows.EARLY[1]) < pd.Timestamp(windows.HELDOUT[0])
    assert windows.SCREEN_WINDOWS == {
        "early_2020_2022": windows.EARLY,
        "heldout_2023_plus": windows.HELDOUT,
    }


def test_stress_windows_are_re_exported_not_copied():
    assert windows.STRESS_WINDOWS is SOURCE_STRESS
    assert "COVID crash" in windows.STRESS_WINDOWS


def test_slice_window_is_inclusive_on_naive_and_aware_indexes():
    naive = pd.Series(range(10), index=pd.date_range("2026-08-05", periods=10, freq="D"))
    out = windows.slice_window(naive, ("2026-08-07", "2026-08-12"))
    assert list(out.index.date.astype(str)) == [
        "2026-08-07", "2026-08-08", "2026-08-09", "2026-08-10", "2026-08-11", "2026-08-12"
    ]
    aware = naive.tz_localize("UTC")
    out_aware = windows.slice_window(aware, ("2026-08-07", "2026-08-12"))
    assert len(out_aware) == 6 and out_aware.index.tz is not None
    heldout = windows.slice_window(naive, windows.HELDOUT)
    assert heldout.index[-1] == pd.Timestamp("2026-08-12")
    assert pd.Timestamp(windows.FROZEN_START) not in heldout.index


def test_slice_window_returns_a_copy_and_works_on_frames():
    frame = pd.DataFrame(
        {"a": range(5)}, index=pd.date_range("2023-01-02", periods=5, freq="B")
    )
    out = windows.slice_window(frame, ("2023-01-03", "2023-01-04"))
    assert list(out["a"]) == [1, 2]
    out.loc[:, "a"] = 99
    assert list(frame["a"]) == [0, 1, 2, 3, 4]


def test_slice_window_rejects_bad_input():
    series = pd.Series([1, 2], index=pd.to_datetime(["2023-01-03", "2023-01-02"]))
    with pytest.raises(ValueError, match="sorted"):
        windows.slice_window(series, ("2023-01-01", "2023-01-05"))
    with pytest.raises(ValueError, match="after"):
        windows.slice_window(series.sort_index(), ("2023-01-05", "2023-01-01"))
