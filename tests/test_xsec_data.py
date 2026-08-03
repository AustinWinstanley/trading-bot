import pandas as pd

from backtest.xsec_data import load


def _write_panel(tmp_path, coverage_by_row):
    """Build a close/volume parquet pair with the given per-row symbol coverage."""
    dates = pd.date_range("2020-01-01", periods=len(coverage_by_row), freq="D")
    symbols = [f"SYM{i}" for i in range(max(coverage_by_row))]
    close = pd.DataFrame(index=dates, columns=symbols, dtype="float64")
    for row, n in enumerate(coverage_by_row):
        close.iloc[row, :n] = 100.0
    volume = close.notna().astype("float64") * 1000
    close.to_parquet(tmp_path / "close.parquet")
    volume.to_parquet(tmp_path / "volume.parquet")
    return dates


def test_load_drops_rows_below_min_symbols(tmp_path):
    dates = _write_panel(tmp_path, [1, 2, 1, 150, 150, 150])
    close, volume = load(tmp_path, min_symbols=100)
    assert len(close) == 3
    assert close.index[0] == dates[3]
    assert len(volume) == 3


def test_load_keeps_all_rows_when_all_dense(tmp_path):
    dates = _write_panel(tmp_path, [150, 150, 150])
    close, volume = load(tmp_path, min_symbols=100)
    assert len(close) == 3
    assert len(volume) == 3


def test_load_min_symbols_is_a_floor_not_a_ceiling(tmp_path):
    _write_panel(tmp_path, [99, 100, 101])
    close, _ = load(tmp_path, min_symbols=100)
    assert len(close) == 2
