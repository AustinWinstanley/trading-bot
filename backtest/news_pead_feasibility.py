"""Point-in-time news feasibility audit for an earnings-drift event study.

This does not claim to backtest true standardized unexpected earnings (SUE):
Alpaca/Benzinga news does not provide versioned pre-announcement consensus
estimates.  It tests whether first-publication timestamps and deterministic
headline labels provide enough single-symbol earnings events to justify a
separate news-conditioned price/volume study.

The default audit window was frozen before collection at 2026-01-01 through
2026-07-31.  It excludes the repository's 2026-08-04-onward final-validation
window.  Promotion to the next research phase requires at least 500 unique,
single-symbol earnings-result headlines and at least 95% usable timestamps.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
from pathlib import Path

import pandas as pd

from engine.data import AlpacaClient, REPO_ROOT

DEFAULT_START = dt.date(2026, 1, 1)
DEFAULT_END = dt.date(2026, 7, 31)
MIN_EVENTS = 500
MIN_TIMESTAMP_COVERAGE = 0.95

RESULT_PATTERNS = (
    re.compile(r"\bearnings\b", re.I),
    re.compile(r"\bfinancial results\b", re.I),
    re.compile(r"\bquarterly results\b", re.I),
    re.compile(r"\breports? (?:q[1-4]|first|second|third|fourth)\b", re.I),
    re.compile(r"\b(?:q[1-4] )?eps\b", re.I),
)
FORWARD_PATTERNS = (
    re.compile(r"\bpreview\b", re.I),
    re.compile(r"\bexpected to\b", re.I),
    re.compile(r"\bwhat to expect\b", re.I),
    re.compile(r"\bestimates?\b", re.I),
    re.compile(r"\bupcoming\b", re.I),
    re.compile(r"\bscheduled\b", re.I),
    re.compile(r"\bearnings (?:call|date|calendar)\b", re.I),
    re.compile(r"\btranscript\b", re.I),
)


def is_earnings_result_headline(headline: str) -> bool:
    """High-precision label for released results, not previews or calendars."""
    text = str(headline or "")
    return (
        any(pattern.search(text) for pattern in RESULT_PATTERNS)
        and not any(pattern.search(text) for pattern in FORWARD_PATTERNS)
    )


def publication_session(value: str | pd.Timestamp) -> str:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize("UTC")
    eastern = timestamp.tz_convert("America/New_York")
    if eastern.weekday() >= 5:
        return "non_session_day"
    minute = eastern.hour * 60 + eastern.minute
    if minute < 9 * 60 + 30:
        return "before_open"
    if minute >= 16 * 60:
        return "after_close"
    return "during_market"


def fetch_news(client: AlpacaClient, start: dt.date, end: dt.date) -> pd.DataFrame:
    rows = []
    token = None
    pages = 0
    while True:
        params = {
            "start": start.isoformat(),
            "end": (end + dt.timedelta(days=1)).isoformat(),
            "sort": "asc",
            "limit": 50,
            "include_content": "false",
        }
        if token:
            params["page_token"] = token
        payload = client._get(client.data_base, "/v1beta1/news", params)
        rows.extend(payload.get("news") or [])
        token = payload.get("next_page_token")
        pages += 1
        if pages % 100 == 0:
            print(f"  fetched {pages} pages / {len(rows):,} articles", flush=True)
        if not token:
            break
    if not rows:
        return pd.DataFrame()
    frame = pd.DataFrame(rows)
    frame = frame.drop_duplicates(subset=["id"], keep="first")
    frame["symbols"] = frame["symbols"].apply(
        lambda value: value if isinstance(value, list) else []
    )
    return frame


def audit(frame: pd.DataFrame, *, start: dt.date, end: dt.date) -> dict:
    total = len(frame)
    if total == 0:
        return {
            "decision": "insufficient_coverage",
            "articles": 0,
            "earnings_result_events": 0,
            "timestamp_coverage": 0.0,
        }
    created = pd.to_datetime(frame.get("created_at"), utc=True, errors="coerce")
    timestamp_coverage = float(created.notna().mean())
    labels = frame["headline"].fillna("").map(is_earnings_result_headline)
    one_symbol = frame["symbols"].map(len).eq(1)
    events = frame.loc[labels & one_symbol & created.notna()].copy()
    event_times = created.loc[events.index]
    events["publication_session"] = event_times.map(publication_session)
    events["symbol"] = events["symbols"].str[0]
    unique_events = events.drop_duplicates(subset=["symbol", "created_at", "headline"])
    enough = (
        len(unique_events) >= MIN_EVENTS
        and timestamp_coverage >= MIN_TIMESTAMP_COVERAGE
    )
    return {
        "decision": (
            "proceed_news_conditioned_event_study"
            if enough else "insufficient_coverage"
        ),
        "strategy": "news-conditioned post-earnings drift feasibility",
        "audit_window": {"start": start.isoformat(), "end": end.isoformat()},
        "articles": total,
        "timestamp_coverage": round(timestamp_coverage, 4),
        "earnings_labeled_articles": int(labels.sum()),
        "single_symbol_earnings_events": len(unique_events),
        "unique_event_symbols": int(unique_events["symbol"].nunique()),
        "events_by_publication_session": {
            str(key): int(value)
            for key, value in unique_events["publication_session"].value_counts().items()
        },
        "pre_registered_gate": {
            "minimum_single_symbol_events": MIN_EVENTS,
            "minimum_timestamp_coverage": MIN_TIMESTAMP_COVERAGE,
            "passed": enough,
        },
        "true_pead_status": {
            "decision": "still_deferred",
            "reason": "No versioned pre-announcement EPS/revenue consensus or contributor count is present in the news feed.",
        },
        "next_study_contract": {
            "signal_inputs": [
                "first publication timestamp",
                "opening gap after publication",
                "first-30-minute abnormal volume",
                "VWAP hold or rejection",
            ],
            "entry": "No earlier than the first completed 30-minute regular-session bar after publication.",
            "controls": [
                "matched non-earnings gaps",
                "generic gap-and-volume rule already rejected in drift_study.json",
                "SPY-adjusted forward returns",
            ],
            "prohibitions": [
                "Do not infer an earnings surprise sign from a headline.",
                "Do not use updated_at or article revisions to move an event earlier.",
                "Do not tune on 2026-08-04-onward data.",
            ],
        },
        "limitations": [
            "Headline regex precision must be manually sampled before a return study.",
            "Benzinga coverage is vendor-dependent and does not guarantee a complete earnings universe.",
            "News symbols use current tickers and do not solve delisted-universe survivorship bias.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", type=dt.date.fromisoformat, default=DEFAULT_START)
    parser.add_argument("--end", type=dt.date.fromisoformat, default=DEFAULT_END)
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()
    if args.end < args.start:
        raise ValueError("end must not precede start")
    cache = REPO_ROOT / "state/news" / (
        f"alpaca_news_{args.start}_{args.end}.parquet"
    )
    if cache.exists() and not args.refresh:
        frame = pd.read_parquet(cache)
        print(f"Loaded {len(frame):,} cached articles from {cache}")
    else:
        frame = fetch_news(AlpacaClient(), args.start, args.end)
        cache.parent.mkdir(parents=True, exist_ok=True)
        frame.to_parquet(cache, index=False)
        print(f"Cached {len(frame):,} articles at {cache}")
    result = audit(frame, start=args.start, end=args.end)
    out = REPO_ROOT / "reports/news_pead_feasibility.json"
    out.write_text(json.dumps(result, indent=2))
    print(f"Decision: {result['decision']}")
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
