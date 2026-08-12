import datetime as dt

import pandas as pd

from backtest.news_pead_feasibility import (
    audit,
    is_earnings_result_headline,
    publication_session,
)


def test_headline_classifier_excludes_previews_and_calls():
    assert is_earnings_result_headline("Acme Reports Q2 EPS $1.20, Sales $4B")
    assert is_earnings_result_headline("Acme Announces Second Quarter Financial Results")
    assert not is_earnings_result_headline("What To Expect From Acme Earnings")
    assert not is_earnings_result_headline("Acme Earnings Call Scheduled For Tuesday")


def test_publication_session_uses_eastern_time():
    assert publication_session("2026-07-15T12:00:00Z") == "before_open"
    assert publication_session("2026-07-15T14:00:00Z") == "during_market"
    assert publication_session("2026-07-15T21:00:00Z") == "after_close"


def test_audit_requires_pre_registered_event_count():
    frame = pd.DataFrame([
        {
            "id": 1,
            "created_at": "2026-07-15T12:00:00Z",
            "headline": "Acme Reports Q2 EPS $1.20",
            "symbols": ["ACME"],
        },
        {
            "id": 2,
            "created_at": "2026-07-15T13:00:00Z",
            "headline": "Market opens higher",
            "symbols": ["SPY"],
        },
    ])
    result = audit(
        frame, start=dt.date(2026, 1, 1), end=dt.date(2026, 7, 31)
    )
    assert result["decision"] == "insufficient_coverage"
    assert result["single_symbol_earnings_events"] == 1
    assert result["true_pead_status"]["decision"] == "still_deferred"
