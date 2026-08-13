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


def test_headline_classifier_excludes_the_preview_template_family_found_2026_08_12():
    """Added after hand-checking a 120-headline real sample against the
    live cached Alpaca/Benzinga news dataset — see
    reports/news_pead_feasibility_precision_check.json. Every example here
    is a real headline from that sample that was a false positive before
    the fix."""
    false_positive_examples = [
        "A Glimpse of FormFactor's Earnings Potential",
        "Earnings Outlook For Cullen/Frost Bankers",
        "Insights into Banco Santander Chile Q4 Earnings",
        "Vishay Intertechnology: Q4 Earnings Insights",
        "Analysts Say Microsoft Stock Is Deeply Undervalued Ahead of Q4 Earnings",
        "Price Over Earnings Overview: Netflix",
        "Exploring Centene's Earnings Expectations",
        "A Look Ahead: Federated Hermes's Earnings Forecast",
        "A Peek at Marriott Intl's Future Earnings",
        "An Overview of National Vision Holdings's Earnings",
    ]
    for headline in false_positive_examples:
        assert not is_earnings_result_headline(headline), headline


def test_headline_classifier_still_includes_genuine_results_and_guidance():
    """The regex fix must not have swallowed the actual result/guidance
    headlines the classifier exists to find — real true-positive examples
    from the same hand-checked sample."""
    true_positive_examples = [
        "CBRE Group Raises FY2026 Adj EPS Guidance from $7.30-$7.60 to $7.60-$7.80 vs $7.46 Est",
        "Nucor Shares Flat Despite Q2 Earnings Beat: Details",
        "Cinemark Holdings Q4 Earnings Breakdown",
        "Element Fleet Management Q1 Adj. EPS $0.35 Up From $0.28 YoY, Sales $323.516M Up From $275.671M YoY",
        "PayPal Holdings Earnings Report: Q4 Overview",
        "Bumble Stock Charges Higher After Q4 Earnings Report",
        "Kingstone Reports Q4 Preliminary Operating Earnings $1.03 - $1.08/Share, FY25 Operating EPS Range $2.71 - $2.79",
    ]
    for headline in true_positive_examples:
        assert is_earnings_result_headline(headline), headline


def test_headline_classifier_known_precision_recall_tradeoff():
    """The 'outlook' pattern trades away two genuine post-release headlines
    to eliminate the much larger 'Earnings Outlook For X' preview-template
    family — documented, not accidental. See
    reports/news_pead_feasibility_precision_check.json's recall_cost note."""
    assert not is_earnings_result_headline("Qorvo Stock Tumbles On Q3 Earnings As Outlook Disappoints")


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
