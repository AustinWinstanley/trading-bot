import datetime as dt

from scripts.zero_dte_shadow import select_zero_dte_surface


def _q(bid, ask):
    return {"latestQuote": {"bp": bid, "ap": ask, "bs": 5, "as": 5}}


def test_selects_executable_defined_risk_condor_and_atm_surface():
    snapshots = {
        "SPY260812C00600000": _q(3.9, 4.0),
        "SPY260812P00600000": _q(3.7, 3.8),
        "SPY260812C00605000": _q(1.9, 2.0),
        "SPY260812C00610000": _q(.4, .5),
        "SPY260812P00595000": _q(1.8, 1.9),
        "SPY260812P00590000": _q(.3, .4),
    }
    row = select_zero_dte_surface(
        snapshots, spot=600, today=dt.date(2026, 8, 12),
        short_distance_pct=.005, wing_width=5,
    )
    assert row["atm_straddle_ask"] == 7.8
    assert row["call_width"] == row["put_width"] == 5
    assert row["executable_credit"] > 0
    assert row["maximum_loss"] < 500
