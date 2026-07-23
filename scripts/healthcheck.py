"""Read-only operational health check for a paper profile."""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sqlite3
from zoneinfo import ZoneInfo

from engine.data import REPO_ROOT, load_env
from engine.execute import Trader
from scripts.run_daily import PROFILES, is_protective_order

ET = ZoneInfo("America/New_York")


def assess_health(
    *,
    account_status: str,
    positions: list[dict],
    open_orders: list[dict],
    fallback_stops: set[str],
    last_snapshot: dt.datetime | None,
    now: dt.datetime,
    max_age_hours: float,
) -> list[str]:
    problems: list[str] = []
    if account_status != "ACTIVE":
        problems.append(f"account status is {account_status!r}, expected ACTIVE")

    if last_snapshot is None:
        problems.append("no journal snapshot exists")
    else:
        if last_snapshot.tzinfo is None:
            last_snapshot = last_snapshot.replace(tzinfo=ET)
        age_hours = (now - last_snapshot.astimezone(now.tzinfo)).total_seconds() / 3600
        if age_hours > max_age_hours:
            problems.append(
                f"last journal snapshot is {age_hours:.1f}h old "
                f"(limit {max_age_hours:.1f}h)"
            )

    protected = {
        str(order.get("symbol"))
        for order in open_orders
        if is_protective_order(order)
    }
    for position in positions:
        symbol = str(position.get("symbol", ""))
        if symbol and symbol not in protected and symbol not in fallback_stops:
            problems.append(f"{symbol}: position has no broker or fallback stop")

    for order in open_orders:
        submitted = order.get("submitted_at")
        if not submitted or is_protective_order(order):
            continue
        try:
            stamp = dt.datetime.fromisoformat(str(submitted).replace("Z", "+00:00"))
            age_hours = (now - stamp.astimezone(now.tzinfo)).total_seconds() / 3600
        except (TypeError, ValueError):
            continue
        if age_hours > 24:
            problems.append(
                f"{order.get('symbol', '?')}: non-protective order "
                f"{order.get('id', '?')} is {age_hours:.1f}h old"
            )
    return problems


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=list(PROFILES), default="base")
    parser.add_argument("--max-age-hours", type=float, default=72.0)
    args = parser.parse_args()

    _, env_suffix, state_suffix = PROFILES[args.profile]
    db_path = REPO_ROOT / "state" / f"paper{state_suffix}.db"

    load_env()
    trader = Trader(
        key=os.environ.get(f"ALPACA_API_KEY{env_suffix}"),
        secret=os.environ.get(f"ALPACA_API_SECRET{env_suffix}"),
    )
    account = trader.get_account()
    positions = trader.get_positions()
    orders = trader.open_orders()

    last_snapshot = None
    fallback_stops: set[str] = set()
    if db_path.exists():
        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT MAX(ts) FROM snapshots").fetchone()
        if row and row[0]:
            last_snapshot = dt.datetime.fromisoformat(row[0])
        fallback_stops = {
            str(row[0]) for row in conn.execute("SELECT symbol FROM stops").fetchall()
        }

    now = dt.datetime.now(ET)
    problems = assess_health(
        account_status=str(account.get("status", "")),
        positions=positions,
        open_orders=orders,
        fallback_stops=fallback_stops,
        last_snapshot=last_snapshot,
        now=now,
        max_age_hours=args.max_age_hours,
    )
    print(
        f"profile={args.profile} equity={account.get('equity')} "
        f"positions={len(positions)} open_orders={len(orders)}"
    )
    if problems:
        for problem in problems:
            print(f"CRITICAL: {problem}")
        raise SystemExit(1)
    print("HEALTHY")


if __name__ == "__main__":
    main()
