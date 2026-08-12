"""Read-only SPY 0DTE surface and defined-risk iron-condor collector."""

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


def select_zero_dte_surface(
    snapshots: dict,
    *,
    spot: float,
    today: dt.date,
    short_distance_pct: float,
    wing_width: float,
) -> dict:
    rows = []
    for symbol, snapshot in snapshots.items():
        parsed = parse_option(symbol)
        quote = snapshot.get("latestQuote") or {}
        if not parsed or parsed[0] != "SPY" or parsed[1] != today:
            continue
        if not all(float(quote.get(key) or 0) > 0 for key in ("bp", "ap", "bs", "as")):
            continue
        rows.append((symbol, parsed[2], parsed[3], quote))
    if not rows:
        raise ValueError("no quoted SPY 0DTE contracts")

    def nearest(kind, target, predicate=lambda _strike: True):
        candidates = [
            row for row in rows if row[1] == kind and predicate(row[2])
        ]
        if not candidates:
            raise ValueError(f"no quoted {kind}s")
        return min(candidates, key=lambda row: abs(row[2] - target))

    atm_call = nearest("call", spot)
    atm_put = nearest("put", spot)
    short_call = nearest("call", spot * (1 + short_distance_pct))
    short_put = nearest("put", spot * (1 - short_distance_pct))
    long_call = nearest(
        "call", short_call[2] + wing_width,
        lambda strike: strike > short_call[2],
    )
    long_put = nearest(
        "put", short_put[2] - wing_width,
        lambda strike: strike < short_put[2],
    )
    call_width = long_call[2] - short_call[2]
    put_width = short_put[2] - long_put[2]
    if call_width <= 0 or put_width <= 0:
        raise ValueError("invalid iron-condor strike ordering")
    credit = (
        float(short_call[3]["bp"]) + float(short_put[3]["bp"])
        - float(long_call[3]["ap"]) - float(long_put[3]["ap"])
    )
    maximum_width = max(call_width, put_width)
    return {
        "atm_call_symbol": atm_call[0],
        "atm_put_symbol": atm_put[0],
        "atm_straddle_ask": float(atm_call[3]["ap"]) + float(atm_put[3]["ap"]),
        "short_call_symbol": short_call[0],
        "long_call_symbol": long_call[0],
        "short_put_symbol": short_put[0],
        "long_put_symbol": long_put[0],
        "call_width": call_width,
        "put_width": put_width,
        "executable_credit": credit,
        "credit_pct_of_width": credit / maximum_width,
        "maximum_loss": max(maximum_width - credit, 0) * 100,
    }


def option_chain(client: Trader, spot: float, today: dt.date) -> dict:
    snapshots = {}
    for kind in ("call", "put"):
        token = None
        for _ in range(MAX_PAGES):
            params = {
                "feed": "indicative", "type": kind,
                "expiration_date": today.isoformat(),
                "strike_price_gte": round(spot * .96, 2),
                "strike_price_lte": round(spot * 1.04, 2),
                "limit": 1000,
            }
            if token:
                params["page_token"] = token
            payload = client._get(
                client.data_base, "/v1beta1/options/snapshots/SPY", params
            )
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
                ts TEXT PRIMARY KEY, profile TEXT, spot REAL,
                atm_straddle_ask REAL, executable_credit REAL,
                credit_pct_of_width REAL, maximum_loss REAL,
                structure_qualified INTEGER, directional_enabled INTEGER,
                raw TEXT)
        """)
        _ensure_column(conn, "observations", "run_slot", "TEXT")
        conn.execute(
            "INSERT OR REPLACE INTO observations "
            "(ts, profile, spot, atm_straddle_ask, executable_credit, "
            "credit_pct_of_width, maximum_loss, structure_qualified, "
            "directional_enabled, raw, run_slot) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (row["ts"], row["profile"], row["spot"], row["atm_straddle_ask"],
             row["executable_credit"], row["credit_pct_of_width"],
             row["maximum_loss"], int(row["structure_qualified"]),
             int(row["directional_enabled"]), json.dumps(row, sort_keys=True),
             row["run_slot"]),
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=("2x",), default="2x")
    args = parser.parse_args()
    cfg = load_config(REPO_ROOT / "config_2x.yaml")
    settings = cfg.sleeves_paper.get("options_experiments", {}).get(
        "zero_dte_surface", {}
    )
    if settings.get("mode", "off") != "shadow":
        print("0DTE shadow off")
        return
    load_env()
    key = os.environ.get("ALPACA_API_KEY_2X")
    secret = os.environ.get("ALPACA_API_SECRET_2X")
    if not (key and secret):
        # Trader() falls back to the unsuffixed ALPACA_API_KEY/SECRET when
        # given None — silently recording "profile": "2x" rows sourced from
        # the base account's equity/positions. Stand down loudly instead,
        # matching scripts/run_daily.py's guard for the same failure mode.
        print("CRITICAL: profile 2x needs ALPACA_API_KEY_2X / "
              "ALPACA_API_SECRET_2X in .env — standing down")
        raise SystemExit(1)
    client = Trader(
        key=key, secret=secret,
        # Runs sequentially with other read-only collectors against a
        # shared 200/min account budget; keep any single script well
        # below that so it can never starve the others or a nearby
        # trading job's own calls.
        max_calls_per_min=40,
    )
    today = dt.datetime.now(ET).date()
    spot = client.latest_price("SPY")
    if not spot:
        raise ValueError("SPY latest price unavailable")
    account = client.get_account()
    try:
        row = select_zero_dte_surface(
            option_chain(client, spot, today), spot=spot, today=today,
            short_distance_pct=float(settings["short_distance_pct"]),
            wing_width=float(settings["wing_width"]),
        )
    except ValueError as exc:
        # Expected after hours or when the indicative feed lacks a complete
        # four-leg market. Missing evidence is not a trading-run failure.
        print(f"0DTE shadow: no valid quoted structure ({exc})")
        return
    now_dt = dt.datetime.now(ET)
    row.update(
        ts=now_dt.isoformat(), profile=args.profile, spot=spot,
        run_slot=run_slot(now_dt),
        directional_enabled=bool(settings.get("directional_enabled", False)),
    )
    equity = float(account["equity"])
    row["structure_qualified"] = bool(
        row["executable_credit"] > 0
        and row["credit_pct_of_width"] >= float(settings["min_credit_pct_of_width"])
        and row["maximum_loss"] <= equity * float(settings["max_loss_pct"])
    )
    record(REPO_ROOT / "state/zero_dte_shadow_2x.db", row)
    print(
        f"0DTE shadow: straddle ask ${row['atm_straddle_ask']:.2f}, "
        f"condor credit ${row['executable_credit']:.2f}, max loss "
        f"${row['maximum_loss']:.0f}, qualified={row['structure_qualified']}, "
        f"directional={row['directional_enabled']}"
    )


if __name__ == "__main__":
    main()
