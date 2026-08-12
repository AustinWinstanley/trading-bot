import datetime as dt
import json

import pytest

from scripts.intraday_options_shadow import qualified_families, select_1dte_vertical


def _snapshot(delta, bid, ask):
    return {"greeks": {"delta": delta}, "latestQuote": {
        "bp": bid, "ap": ask, "bs": 10, "as": 10,
    }}


def test_research_interlock_requires_explicit_advanced_family(tmp_path):
    report = tmp_path / "report.json"
    report.write_text(json.dumps({"advanced_families": []}))
    assert qualified_families(report) == []
    report.write_text(json.dumps({"advanced_families": ["orb"]}))
    assert qualified_families(report) == ["orb"]


def test_selects_defined_risk_1dte_call_spread():
    snapshots = {
        "SPY260813C00600000": _snapshot(.61, 3.8, 4.0),
        "SPY260813C00605000": _snapshot(.36, 1.8, 2.0),
        "SPY260814C00600000": _snapshot(.60, 5.0, 5.2),
        "SPY260814C00605000": _snapshot(.35, 3.0, 3.2),
    }
    row = select_1dte_vertical(
        snapshots, underlying="SPY", direction=1, today=dt.date(2026, 8, 12)
    )
    assert row["expiration_date"] == "2026-08-13"
    assert row["direction"] == "bull_call"
    assert row["net_debit"] == 2.2
    assert row["maximum_loss"] == pytest.approx(220)
