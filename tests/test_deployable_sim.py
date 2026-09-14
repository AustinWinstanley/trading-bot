"""simulate_targets: the live gate's rules on synthetic panels, plus the
full-panel reproduction of build_deployable_stream when the cache exists."""

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from backtest.deployable_sim import (
    DIAGNOSTIC_COLUMNS,
    mom_ls_target_weights,
    simulate_targets,
)

EQUITY = 10_000.0


def _panel(prices: dict[str, list[float]]) -> pd.DataFrame:
    n = len(next(iter(prices.values())))
    index = pd.date_range("2025-01-06", periods=n, freq="B")
    return pd.DataFrame(prices, index=index, dtype="float64")


def _targets(close: pd.DataFrame, weights: dict[str, list[float]]) -> pd.DataFrame:
    return pd.DataFrame(weights, index=close.index, dtype="float64")


def _run(close, targets, **kwargs):
    kwargs.setdefault("record_positions", True)
    r, diag = simulate_targets(targets, close, **kwargs)
    return r, diag, diag.attrs["positions"]


def test_whole_share_short_floor_rejects_sub_share_targets():
    close = _panel({"S": [40.0] * 4, "T": [70.0] * 4})
    targets = _targets(close, {"S": [-0.006] * 4, "T": [-0.006] * 4})  # -$60 each
    _, diag, pos = _run(close, targets)
    assert pos["S"].iloc[0] == -1.0            # floor($60 / $40) = 1 whole share
    assert (pos["T"] == 0).all()               # floor($60 / $70) = 0 -> rejected
    assert diag["rejected_whole_share"].iloc[0] == 1
    assert diag["short_exposure"].iloc[0] == 40.0
    # Borrow is charged on the realized short gross, from the next day.
    assert diag["borrow_cost"].iloc[0] == 0.0
    assert diag["borrow_cost"].iloc[1] == pytest.approx(40.0 * 0.03 / 252)


def test_band_cap_forces_a_large_drift_to_trade():
    close = _panel({"SPY": [100.0] * 3})
    targets = _targets(close, {"SPY": [0.61, 0.75, 0.75]})
    elevated = {"elevated_cap": ({"SPY"}, 0.80)}
    _, uncapped, pos_uncapped = _run(close, targets, band_equity_cap=None, **elevated)
    _, capped, pos_capped = _run(close, targets, band_equity_cap=0.05, **elevated)
    # $6,100 held vs ~$7,490 target: a $1,390 gap sits under the 20% band
    # ($1,498) so the pure fractional band never trades it...
    assert uncapped["trades"].iloc[1] == 0
    assert pos_uncapped["SPY"].iloc[1] == pytest.approx(61.0)
    # ...while the 5%-of-equity cap ($499) makes it trade the same day.
    assert capped["trades"].iloc[1] == 1
    assert capped["long_exposure"].iloc[1] == pytest.approx(0.75 * capped["equity"].iloc[0], rel=1e-6)


def test_position_cap_binds_unless_elevated():
    close = _panel({"SPY": [100.0] * 2})
    targets = _targets(close, {"SPY": [0.75] * 2})
    _, plain, _ = _run(close, targets)
    assert plain["long_exposure"].iloc[0] == pytest.approx(0.15 * EQUITY)
    assert plain["shrunk_notional"].iloc[0] == pytest.approx(0.60 * EQUITY)
    _, elevated, _ = _run(close, targets, elevated_cap=({"SPY"}, 0.80))
    assert elevated["long_exposure"].iloc[0] == pytest.approx(0.75 * EQUITY)


def test_leveraged_cap_shrinks_only_the_leveraged_symbol():
    close = _panel({"TQQQ": [50.0] * 2, "QQQ": [50.0] * 2})
    targets = _targets(close, {"TQQQ": [0.30] * 2, "QQQ": [0.30] * 2})
    _, diag, pos = _run(
        close, targets, leveraged_symbols={"TQQQ"}, leveraged_cap_pct=0.20,
        position_cap_pct=0.50,
    )
    assert pos["TQQQ"].iloc[0] == pytest.approx(0.20 * EQUITY / 50)
    assert pos["QQQ"].iloc[0] == pytest.approx(0.30 * EQUITY / 50)
    assert diag["shrunk_notional"].iloc[0] == pytest.approx(0.10 * EQUITY)


def test_full_exit_always_trades_even_below_min_order_notional():
    close = _panel({"X": [10.0, 10.0, 3.0, 3.0]})
    targets = _targets(close, {"X": [0.005, 0.005, 0.0, 0.0]})  # $50 -> 5 shares
    _, live, pos_live = _run(close, targets)
    assert pos_live["X"].iloc[1] == pytest.approx(5.0)
    assert pos_live["X"].iloc[2] == 0.0            # $15 remnant still exits
    assert live["trades"].iloc[2] == 1
    _, compat, pos_compat = _run(close, targets, deployable_compat=True)
    assert pos_compat["X"].iloc[3] == pytest.approx(5.0)  # the old simulator parks it


def test_long_only_frame_buys_fractional_shares_and_compounds():
    close = _panel({"A": [101.0, 103.0, 102.0, 104.0], "B": [50.0, 49.0, 50.0, 52.0]})
    targets = _targets(close, {"A": [0.6] * 4, "B": [0.4] * 4})
    r, diag, pos = _run(close, targets, position_cap_pct=1.0, cost_bps_by_symbol=15.0)
    qty_a, qty_b = pos["A"].iloc[0], pos["B"].iloc[0]
    assert qty_a == pytest.approx(6000 / 101) and abs(qty_a - round(qty_a)) > 1e-6
    assert qty_b == pytest.approx(80.0)
    # Day 0: fully invested, pays 15bps on $10k.
    assert r.iloc[0] == pytest.approx(-0.0015)
    assert diag["cash"].iloc[0] == pytest.approx(-15.0)
    # Day 1: pure mark-to-market, no rebalance (drift inside the band). The
    # $15 of commissions left the book $15 long of equity, and that $15 is
    # financed at the margin rate like any other long exposure above equity.
    equity_0 = EQUITY * (1 + r.iloc[0])
    pnl_1 = qty_a * 2.0 + qty_b * -1.0
    margin_1 = 15.0 * 0.05 / 252
    assert diag["trades"].iloc[1] == 0
    assert diag["margin_cost"].iloc[1] == pytest.approx(margin_1)
    assert r.iloc[1] == pytest.approx((pnl_1 - margin_1) / equity_0)
    assert diag["equity"].iloc[-1] == pytest.approx(EQUITY * (1 + r).prod())
    assert list(diag.columns) == DIAGNOSTIC_COLUMNS
    assert (diag["borrow_cost"] == 0).all()


def test_margin_is_charged_on_long_exposure_above_equity():
    close = _panel({"A": [100.0] * 3})
    targets = _targets(close, {"A": [1.5] * 3})
    _, diag, _ = _run(close, targets, gross_leverage=2.0, position_cap_pct=2.0)
    assert diag["long_exposure"].iloc[0] == pytest.approx(1.5 * EQUITY)
    financed = 1.5 * EQUITY - diag["equity"].iloc[0]
    assert diag["margin_cost"].iloc[1] == pytest.approx(financed * 0.05 / 252)


def test_no_averaging_down_rejects_a_losing_restoration():
    close = _panel({"G": [10.0, 10.0, 7.0, 7.0]})
    targets = _targets(close, {"G": [0.01] * 4})  # $100 -> 10 shares
    _, diag, pos = _run(close, targets)
    assert pos["G"].iloc[2] == pytest.approx(10.0)
    assert diag["rejected_averaging_down"].iloc[2] == 1
    _, allowed, pos_allowed = _run(close, targets, allow_averaging_down=True)
    assert pos_allowed["G"].iloc[2] > 10.0
    assert allowed["rejected_averaging_down"].sum() == 0


def test_sign_flip_closes_first_and_opens_later():
    close = _panel({"F": [20.0] * 4})
    # $105 long = 5.25 fractional shares; the short leg floors to 5 whole.
    targets = _targets(close, {"F": [0.0105, 0.0105, -0.0105, -0.0105]})
    _, live, pos_live = _run(close, targets)
    assert pos_live["F"].tolist() == pytest.approx([5.25, 5.25, 0.0, -5.0])
    _, compat, pos_compat = _run(close, targets, deployable_compat=True)
    assert pos_compat["F"].tolist() == pytest.approx([5.25, 5.25, -5.0, -5.0])


def test_buying_headroom_ignores_same_day_sells():
    close = _panel({"A": [100.0] * 4, "B": [100.0] * 4})
    targets = _targets(close, {"A": [1.0, 1.0, 0.0, 0.0], "B": [0.0, 0.0, 1.0, 1.0]})
    _, diag, pos = _run(close, targets, position_cap_pct=1.0)
    assert pos["A"].iloc[1] == pytest.approx(100.0)
    # Rotation day: A's exit goes through, B's buy has no headroom yet.
    assert pos["A"].iloc[2] == 0.0 and pos["B"].iloc[2] == 0.0
    assert diag["rejected_headroom"].iloc[2] == 1
    assert pos["B"].iloc[3] > 0.0


def test_partial_cover_below_one_share_promotes_to_full_close():
    # 5 shares short at $10 against a $50 target. At $11.50 the book is
    # $57.50 short: the ~$7.60 excess clears a 5% band but is only 0.66 of
    # a share, so the live gate promotes the sub-share cover to a full
    # close (engine/risk._exit_quantity) rather than leave a fractional short.
    close = _panel({"S": [10.0, 10.0, 11.5, 11.5]})
    targets = _targets(close, {"S": [-0.005] * 4})  # -$50
    _, diag, pos = _run(close, targets, rebalance_band=0.05, min_order_notional=5.0)
    assert pos["S"].iloc[1] == -5.0
    assert pos["S"].iloc[2] == 0.0
    assert diag["trades"].iloc[2] == 1


def test_rejects_targets_off_the_panel():
    close = _panel({"A": [100.0] * 3})
    bad_dates = pd.DataFrame({"A": [0.1]}, index=[pd.Timestamp("2030-01-01")])
    with pytest.raises(ValueError, match="target dates"):
        simulate_targets(bad_dates, close)
    bad_symbol = _targets(close, {"Z": [0.1] * 3})
    with pytest.raises(ValueError, match="target symbols"):
        simulate_targets(bad_symbol, close)


def test_exogenous_equity_path_does_not_compound():
    close = _panel({"A": [100.0, 110.0, 121.0]})
    targets = _targets(close, {"A": [0.1] * 3})
    r, diag, _ = _run(close, targets, equity=pd.Series(EQUITY, index=close.index))
    assert (diag["equity"] == EQUITY).all()
    assert r.iloc[1] == pytest.approx((10 * 10.0) / EQUITY)


# ---------------------------------------------------------------------------
# Full-panel reproduction of build_deployable_stream (AGENTS.md: validate a
# new simulator against the accepted one before trusting its variants).
# ---------------------------------------------------------------------------

PANEL = Path("state/xsec/close.parquet")
UNIVERSE = Path("state/universe_classified.json")


@pytest.fixture(scope="module")
def stock_panel():
    from backtest.production_portfolio import norm_index
    from backtest.xsec_data import load

    close_all, volume_all = load()
    close_all, volume_all = norm_index(close_all), norm_index(volume_all)
    stocks = [s for s in json.loads(UNIVERSE.read_text())["stocks"] if s in close_all]
    return close_all[stocks], volume_all[stocks]


@pytest.mark.skipif(
    not (PANEL.exists() and UNIVERSE.exists()), reason="cross-sectional panel not cached"
)
@pytest.mark.parametrize("profile", ["base", "2x"])
def test_reproduces_build_deployable_stream_on_the_cached_panel(stock_panel, profile):
    from backtest.deployable_momentum import build_deployable_stream
    from backtest.production_portfolio import SHORT_BORROW, TD
    from backtest.short_capacity_study import MOM_ACCOUNT_MULTIPLIER, STARTING_EQUITY

    close32, volume32 = stock_panel
    close, volume = close32.astype("float64"), volume32.astype("float64")
    mult = MOM_ACCOUNT_MULTIPLIER[profile]
    reference, diag = build_deployable_stream(close, volume, account_multiplier=mult)
    targets = mom_ls_target_weights(close, volume, account_multiplier=mult)
    constant_equity = pd.Series(STARTING_EQUITY, index=close.index)

    sim, sim_diag = simulate_targets(
        targets, close, equity=constant_equity, band_equity_cap=None,
        borrow_rate=SHORT_BORROW, margin_rate=0.0, deployable_compat=True,
    )
    expected = reference.returns * mult - reference.short_gross * mult * SHORT_BORROW / TD
    assert (sim - expected).abs().max() < 1e-8
    assert int(sim_diag["trades"].sum()) == diag.trades
    assert int(sim_diag["rejected_averaging_down"].sum()) == diag.rejected_restorations

    # The studies call the older simulator on the float32 panel; that costs
    # nothing beyond float32 rounding of the daily returns.
    reference32, _ = build_deployable_stream(close32, volume32, account_multiplier=mult)
    expected32 = reference32.returns * mult - reference32.short_gross * mult * SHORT_BORROW / TD
    assert (sim - expected32).abs().max() < 1e-8

    # And the live rules are a genuinely different simulator, not a relabel.
    live, _ = simulate_targets(
        targets, close, equity=constant_equity, borrow_rate=SHORT_BORROW, margin_rate=0.0,
    )
    assert (live - expected).abs().max() > 1e-4
