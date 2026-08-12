"""Fetch/cache and audit the common five-minute ETF research panel."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from backtest.intraday import SYMBOLS, load_or_fetch

START = dt.date(2024, 2, 1)
END = dt.date(2026, 8, 1)


def main() -> None:
    frames = load_or_fetch(start=START, end=END)
    coverage = {}
    for symbol in SYMBOLS:
        frame = frames[symbol]
        counts = frame.groupby("session").size() if len(frame) else []
        coverage[symbol] = {
            "bars": len(frame),
            "sessions": int(frame["session"].nunique()) if len(frame) else 0,
            "first_bar": frame.index.min().isoformat() if len(frame) else None,
            "last_bar": frame.index.max().isoformat() if len(frame) else None,
            "median_bars_per_session": float(counts.median()) if len(frame) else 0,
            "complete_78_bar_sessions": int((counts == 78).sum()) if len(frame) else 0,
        }
    minimum = min(row["sessions"] for row in coverage.values())
    payload = {
        "decision": "usable_for_screening" if minimum >= 400 else "insufficient_coverage",
        "feed": "Alpaca account-configured stock feed (IEX on free tier)",
        "timeframe": "5Min",
        "window": {"start": START.isoformat(), "end_exclusive": END.isoformat()},
        "coverage": coverage,
        "execution_contract": {
            "signal": "completed bar only",
            "entry": "next bar open",
            "exit": "same regular session",
            "primary_cost_bps_per_leg": 2.0,
            "stress_cost_bps_per_leg": 5.0,
        },
        "limitations": [
            "Free-tier IEX represents one venue rather than consolidated SIP activity.",
            "Five-minute bars do not resolve within-bar stop/target ordering.",
            "The panel starts in 2024 and cannot validate COVID or the 2022 bear market.",
        ],
    }
    out = Path("reports/intraday_data_audit.json")
    out.write_text(json.dumps(payload, indent=2))
    print(f"Decision: {payload['decision']}")
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
