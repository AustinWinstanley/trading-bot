"""Read-only SPY straddle observations before scheduled macro events."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sqlite3
from pathlib import Path
from zoneinfo import ZoneInfo

from engine.config import load_config
from engine.data import REPO_ROOT, load_env
from engine.execute import Trader
from scripts.momentum_options_shadow import MAX_PAGES, parse_option, run_slot

ET = ZoneInfo("America/New_York")


def select_atm_straddle(
    snapshots: dict,
    *,
    spot: float,
    event_date: dt.date,
) -> dict:
    rows = []
    for symbol, snapshot in snapshots.items():
        parsed = parse_option(symbol)
        if not parsed:
            continue
        root, expiry, kind, strike = parsed
        quote = snapshot.get("latestQuote") or {}
        if root != "SPY" or expiry < event_date:
            continue
        if not all(float(quote.get(k) or 0) > 0 for k in ("bp", "ap", "bs", "as")):
            continue
        rows.append((symbol, expiry, kind, strike, snapshot))
    expiries = sorted({row[1] for row in rows})
    if not expiries:
        raise ValueError("no quoted expiration on or after event")
    expiry = expiries[0]
    same = [row for row in rows if row[1] == expiry]
    common = sorted(
        {r[3] for r in same if r[2] == "call"}
        & {r[3] for r in same if r[2] == "put"}
    )
    if not common:
        raise ValueError("no common quoted call/put strike")
    strike = min(common, key=lambda value: abs(value - spot))
    call = next(r for r in same if r[2] == "call" and r[3] == strike)
    put = next(r for r in same if r[2] == "put" and r[3] == strike)
    call_q, put_q = call[4]["latestQuote"], put[4]["latestQuote"]
    debit = float(call_q["ap"]) + float(put_q["ap"])
    return {
        "expiration_date": expiry.isoformat(),
        "strike": strike,
        "call_symbol": call[0],
        "call_ask": float(call_q["ap"]),
        "call_delta": (call[4].get("greeks") or {}).get("delta"),
        "put_symbol": put[0],
        "put_ask": float(put_q["ap"]),
        "put_delta": (put[4].get("greeks") or {}).get("delta"),
        "straddle_debit": debit,
        "implied_break_even_move_pct": debit / spot,
    }


def chain(client: Trader, spot: float, today: dt.date, last_event: dt.date) -> dict:
    snapshots = {}
    for kind in ("call", "put"):
        token = None
        for _ in range(MAX_PAGES):
            params = {
                "feed": "indicative", "type": kind,
                "expiration_date_gte": today.isoformat(),
                "expiration_date_lte": (last_event + dt.timedelta(days=14)).isoformat(),
                "strike_price_gte": round(spot * .96, 2),
                "strike_price_lte": round(spot * 1.04, 2), "limit": 1000,
            }
            if token:
                params["page_token"] = token
            payload = client._get(client.data_base, "/v1beta1/options/snapshots/SPY", params)
            snapshots.update(payload.get("snapshots") or {})
            next_token = payload.get("next_page_token")
            if not next_token or next_token == token:
                break
            token = next_token
    return snapshots


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, kind: str) -> None:
    """Additive migration so an already-deployed table gains new columns
    without losing rows — see scripts/options_shadow.py's _ensure_column."""
    existing = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")


def record(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS observations(
                ts TEXT, profile TEXT, event_name TEXT, event_date TEXT,
                days_to_event INTEGER, spot REAL, expiration_date TEXT,
                strike REAL, call_symbol TEXT, put_symbol TEXT,
                straddle_debit REAL, implied_break_even_move_pct REAL, raw TEXT,
                PRIMARY KEY(ts, event_name, event_date))
        """)
        _ensure_column(conn, "observations", "run_slot", "TEXT")
        conn.execute(
            "INSERT OR REPLACE INTO observations "
            "(ts, profile, event_name, event_date, days_to_event, spot, "
            "expiration_date, strike, call_symbol, put_symbol, straddle_debit, "
            "implied_break_even_move_pct, raw, run_slot) "
            "VALUES (" + ",".join("?" * 14) + ")",
            (row["ts"], row["profile"], row["event_name"], row["event_date"],
             row["days_to_event"], row["spot"], row["expiration_date"],
             row["strike"], row["call_symbol"], row["put_symbol"],
             row["straddle_debit"], row["implied_break_even_move_pct"],
             json.dumps(row, sort_keys=True), row["run_slot"]),
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=("base", "2x"), default="2x")
    args = parser.parse_args()
    config_name = "config_2x.yaml" if args.profile == "2x" else "config.yaml"
    cfg = load_config(REPO_ROOT / config_name)
    settings = cfg.sleeves_paper.get("options_experiments", {}).get("event_volatility", {})
    if settings.get("mode", "off") != "shadow":
        print("event volatility shadow off")
        return
    today = dt.datetime.now(ET).date()
    window = int(settings["observation_window_days"])
    events = [
        (row["name"], dt.date.fromisoformat(row["date"]))
        for row in settings["events"]
        if 0 <= (dt.date.fromisoformat(row["date"]) - today).days <= window
    ]
    if not events:
        print("event volatility shadow: no event inside observation window")
        return
    load_env()
    suffix = "_2X" if args.profile == "2x" else ""
    key = os.environ.get("ALPACA_API_KEY" + suffix)
    secret = os.environ.get("ALPACA_API_SECRET" + suffix)
    if suffix and not (key and secret):
        # Trader() falls back to the unsuffixed ALPACA_API_KEY/SECRET when
        # given None — silently recording "profile": "2x" rows sourced from
        # the base account's equity/positions. Stand down loudly instead,
        # matching scripts/run_daily.py's guard for the same failure mode.
        print(f"CRITICAL: profile {args.profile} needs ALPACA_API_KEY{suffix} / "
              f"ALPACA_API_SECRET{suffix} in .env — standing down")
        raise SystemExit(1)
    client = Trader(
        key=key, secret=secret,
        # Runs sequentially with other read-only collectors against a
        # shared 200/min account budget; keep any single script well
        # below that so it can never starve the others or a nearby
        # trading job's own calls.
        max_calls_per_min=40,
    )
    spot = client.latest_price("SPY")
    if not spot:
        raise ValueError("SPY latest price unavailable")
    snapshots = chain(client, spot, today, max(date for _, date in events))
    out = REPO_ROOT / "state" / f"event_volatility_shadow_{args.profile}.db"
    now_dt = dt.datetime.now(ET)
    now = now_dt.isoformat()
    for name, date in events:
        row = select_atm_straddle(snapshots, spot=spot, event_date=date)
        row.update(ts=now, profile=args.profile, event_name=name, run_slot=run_slot(now_dt),
                   event_date=date.isoformat(), days_to_event=(date - today).days,
                   spot=spot)
        record(out, row)
        print(f"event shadow {name} {date}: {row['days_to_event']}d, "
              f"debit=${row['straddle_debit']:.2f}, implied move "
              f"{row['implied_break_even_move_pct']:.2%}")


if __name__ == "__main__":
    main()
