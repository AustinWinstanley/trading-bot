"""Guarded 1DTE directional option-spread shadow translator.

The module can translate an independently qualified intraday ETF direction
into a defined-risk debit spread.  It intentionally has no order method.  The
CLI checks the committed research decision before loading credentials; with no
qualified family it stands down without contacting Alpaca.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import numpy as np

from engine.data import REPO_ROOT
from scripts.momentum_options_shadow import parse_option

RESEARCH_REPORT = REPO_ROOT / "reports/intraday_strategy_study.json"


def qualified_families(path: Path = RESEARCH_REPORT) -> list[str]:
    if not path.exists():
        return []
    payload = json.loads(path.read_text())
    return list(payload.get("advanced_families") or [])


def select_1dte_vertical(
    snapshots: dict,
    *,
    underlying: str,
    direction: int,
    today: dt.date,
    long_delta: float = 0.60,
    short_delta: float = 0.35,
) -> dict:
    """Select the nearest 1DTE-or-later call/put debit spread at quote sides."""
    kind = "call" if direction > 0 else "put"
    rows = []
    for symbol, snapshot in snapshots.items():
        parsed = parse_option(symbol)
        delta = (snapshot.get("greeks") or {}).get("delta")
        quote = snapshot.get("latestQuote") or {}
        if not parsed or delta is None or not np.isfinite(float(delta)):
            continue
        root, expiry, parsed_kind, strike = parsed
        if root != underlying or parsed_kind != kind or (expiry - today).days < 1:
            continue
        if not all(float(quote.get(key) or 0) > 0 for key in ("bp", "ap", "bs", "as")):
            continue
        rows.append((symbol, expiry, strike, snapshot, abs(float(delta))))
    if not rows:
        raise ValueError("no quoted 1DTE-or-later contracts with Greeks")
    expiry = min({row[1] for row in rows}, key=lambda value: (value - today).days)
    same = [row for row in rows if row[1] == expiry]
    long_leg = min(same, key=lambda row: abs(row[4] - long_delta))
    short_candidates = [
        row for row in same
        if (row[2] > long_leg[2] if direction > 0 else row[2] < long_leg[2])
    ]
    if not short_candidates:
        raise ValueError("no correctly ordered short leg")
    short_leg = min(short_candidates, key=lambda row: abs(row[4] - short_delta))
    long_quote = long_leg[3]["latestQuote"]
    short_quote = short_leg[3]["latestQuote"]
    debit = float(long_quote["ap"]) - float(short_quote["bp"])
    width = abs(short_leg[2] - long_leg[2])
    return {
        "underlying": underlying,
        "direction": "bull_call" if direction > 0 else "bear_put",
        "expiration_date": expiry.isoformat(),
        "long_symbol": long_leg[0],
        "short_symbol": short_leg[0],
        "long_delta": long_leg[4],
        "short_delta": short_leg[4],
        "net_debit": debit,
        "maximum_loss": max(debit, 0) * 100,
        "maximum_profit": max(width - debit, 0) * 100,
    }


def main() -> None:
    families = qualified_families()
    if not families:
        print("1DTE directional shadow: standing down; no intraday family passed research")
        return
    print(
        "1DTE directional shadow: research qualifier present, but live signal "
        "collection requires a separately reviewed intraday runner: "
        + ", ".join(families)
    )


if __name__ == "__main__":
    main()
