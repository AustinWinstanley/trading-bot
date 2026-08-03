import pandas as pd
import pytest

from backtest.fund_signals import _trailing_4q_sum, cik_map_coverage


def _quarterly(symbol, dates, values):
    return pd.DataFrame({
        "symbol": symbol,
        "avail_date": pd.to_datetime(dates),
        "value": values,
    })


class TestTrailing4qSum:
    def test_requires_four_quarters_before_producing_a_value(self):
        q = _quarterly(
            "AAA",
            ["2020-01-01", "2020-04-01", "2020-07-01"],
            [10.0, 20.0, 30.0],
        )
        out = _trailing_4q_sum(q)
        assert out.empty

    def test_sums_the_trailing_four_quarters_once_available(self):
        q = _quarterly(
            "AAA",
            ["2020-01-01", "2020-04-01", "2020-07-01", "2020-10-01"],
            [10.0, 20.0, 30.0, 40.0],
        )
        out = _trailing_4q_sum(q)
        assert len(out) == 1
        assert out.iloc[0]["value"] == pytest.approx(100.0)
        assert out.iloc[0]["avail_date"] == pd.Timestamp("2020-10-01")

    def test_rolls_forward_as_new_quarters_land(self):
        q = _quarterly(
            "AAA",
            [
                "2020-01-01", "2020-04-01", "2020-07-01", "2020-10-01",
                "2021-01-01",
            ],
            [10.0, 20.0, 30.0, 40.0, 50.0],
        )
        out = _trailing_4q_sum(q)
        assert len(out) == 2
        # Second window drops the oldest (10) and adds the newest (50).
        assert out.iloc[1]["value"] == pytest.approx(20 + 30 + 40 + 50)

    def test_symbols_are_independent(self):
        a = _quarterly(
            "AAA",
            ["2020-01-01", "2020-04-01", "2020-07-01", "2020-10-01"],
            [1.0, 1.0, 1.0, 1.0],
        )
        b = _quarterly(
            "BBB",
            ["2020-01-01", "2020-04-01", "2020-07-01", "2020-10-01"],
            [100.0, 100.0, 100.0, 100.0],
        )
        out = _trailing_4q_sum(pd.concat([a, b], ignore_index=True))
        values = dict(zip(out["symbol"], out["value"]))
        assert values["AAA"] == pytest.approx(4.0)
        assert values["BBB"] == pytest.approx(400.0)

    def test_duplicate_filing_on_same_date_keeps_the_last(self):
        q = _quarterly(
            "AAA",
            ["2020-01-01", "2020-04-01", "2020-07-01", "2020-10-01", "2020-10-01"],
            [10.0, 20.0, 30.0, 40.0, 45.0],  # restatement on the same date
        )
        out = _trailing_4q_sum(q)
        assert len(out) == 1
        assert out.iloc[0]["value"] == pytest.approx(10 + 20 + 30 + 45)

    def test_empty_input_returns_empty(self):
        empty = pd.DataFrame(columns=["symbol", "avail_date", "value"])
        out = _trailing_4q_sum(empty)
        assert out.empty


class TestCikMapCoverage:
    def test_reports_exact_counts_and_pct(self):
        facts = pd.DataFrame({"cik": [1, 2, 3, 4]})
        cmap = pd.DataFrame({"cik": [1, 2], "symbol": ["AAA", "BBB"]})
        result = cik_map_coverage(facts, cmap)
        assert result["total_ciks_with_facts"] == 4
        assert result["ciks_matched_to_a_current_ticker"] == 2
        assert result["coverage_pct"] == 50.0

    def test_full_coverage(self):
        facts = pd.DataFrame({"cik": [1, 2]})
        cmap = pd.DataFrame({"cik": [1, 2, 3], "symbol": ["AAA", "BBB", "CCC"]})
        result = cik_map_coverage(facts, cmap)
        assert result["coverage_pct"] == 100.0

    def test_empty_facts_does_not_divide_by_zero(self):
        facts = pd.DataFrame({"cik": []})
        cmap = pd.DataFrame({"cik": [1], "symbol": ["AAA"]})
        result = cik_map_coverage(facts, cmap)
        assert result["coverage_pct"] == 0.0
