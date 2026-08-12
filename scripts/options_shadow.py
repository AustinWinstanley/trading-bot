"""Read-only live option quote collector for paper-only experiments.

No method in this module submits, replaces, cancels, exercises, or otherwise
mutates a broker account. `shadow` means data collection only.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sqlite3
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

from engine.config import load_config
from engine.data import REPO_ROOT, load_env
from engine.execute import Trader

ET = ZoneInfo("America/New_York")
OCC = re.compile(r"^SPY(?P<expiry>\d{6})P(?P<strike>\d{8})$")


def parse_contract(symbol: str) -> tuple[dt.date, float] | None:
    match = OCC.match(symbol)
    if not match:
        return None
    expiry = dt.datetime.strptime(match.group("expiry"), "%y%m%d").date()
    strike = int(match.group("strike")) / 1000
    return expiry, strike


def select_quote_pair(
    snapshots: dict,
    *,
    spot: float,
    today: dt.date,
    target_dte: int,
    short_moneyness: float,
    width: float,
) -> dict:
    rows = []
    for symbol, snapshot in snapshots.items():
        parsed = parse_contract(symbol)
        if not parsed:
            continue
        expiry, strike = parsed
        if expiry.weekday() != 4 or not 15 <= expiry.day <= 21:
            continue
        rows.append((symbol, expiry, strike, snapshot))
    if not rows:
        raise ValueError("no standard-monthly SPY put snapshots")
    expiry = min({r[1] for r in rows}, key=lambda d: abs((d - today).days - target_dte))
    same_expiry = [r for r in rows if r[1] == expiry]
    short = min(same_expiry, key=lambda r: abs(r[2] - spot * short_moneyness))
    lower = [r for r in same_expiry if np.isclose(r[2], short[2] - width)]
    if not lower:
        raise ValueError(f"no exact ${width:g} lower strike for {short[0]}")
    long = lower[0]
    short_quote = short[3].get("latestQuote") or {}
    long_quote = long[3].get("latestQuote") or {}
    short_bid = float(short_quote.get("bp") or 0)
    long_ask = float(long_quote.get("ap") or 0)
    executable_credit = short_bid - long_ask
    maximum_loss = (width - executable_credit) * 100
    return {
        "expiration_date": expiry.isoformat(),
        "short_symbol": short[0],
        "short_strike": short[2],
        "short_bid": short_bid,
        "short_ask": float(short_quote.get("ap") or 0),
        "short_quote_ts": short_quote.get("t"),
        "short_delta": (short[3].get("greeks") or {}).get("delta"),
        "short_iv": short[3].get("impliedVolatility"),
        "long_symbol": long[0],
        "long_strike": long[2],
        "long_bid": float(long_quote.get("bp") or 0),
        "long_ask": long_ask,
        "long_quote_ts": long_quote.get("t"),
        "long_delta": (long[3].get("greeks") or {}).get("delta"),
        "long_iv": long[3].get("impliedVolatility"),
        "executable_credit": executable_credit,
        "maximum_loss": maximum_loss,
    }


def select_delta_quote_pair(
    snapshots: dict,
    *,
    today: dt.date,
    target_dte: int,
    target_delta: float,
    width: float,
) -> dict:
    """Select the standard-monthly short put nearest a predeclared delta."""
    rows = []
    for symbol, snapshot in snapshots.items():
        parsed = parse_contract(symbol)
        greeks = snapshot.get("greeks") or {}
        delta = greeks.get("delta")
        if not parsed or delta is None or not np.isfinite(float(delta)):
            continue
        expiry, strike = parsed
        if expiry.weekday() != 4 or not 15 <= expiry.day <= 21:
            continue
        rows.append((symbol, expiry, strike, snapshot))
    if not rows:
        raise ValueError("no standard-monthly SPY puts with Greeks")
    expiry = min({r[1] for r in rows}, key=lambda d: abs((d - today).days - target_dte))
    same_expiry = [r for r in rows if r[1] == expiry]
    pairs = []
    for short in same_expiry:
        lower = [r for r in same_expiry if np.isclose(r[2], short[2] - width)]
        if lower:
            delta = abs(float((short[3].get("greeks") or {})["delta"]))
            pairs.append((abs(delta - target_delta), short, lower[0]))
    if not pairs:
        raise ValueError(f"no exact ${width:g} put pairs with Greeks")
    _, short, long = min(pairs, key=lambda row: row[0])
    short_quote = short[3].get("latestQuote") or {}
    long_quote = long[3].get("latestQuote") or {}
    short_bid = float(short_quote.get("bp") or 0)
    long_ask = float(long_quote.get("ap") or 0)
    executable_credit = short_bid - long_ask
    return {
        "expiration_date": expiry.isoformat(),
        "short_symbol": short[0],
        "short_strike": short[2],
        "short_bid": short_bid,
        "short_ask": float(short_quote.get("ap") or 0),
        "short_quote_ts": short_quote.get("t"),
        "short_delta": (short[3].get("greeks") or {}).get("delta"),
        "short_iv": short[3].get("impliedVolatility"),
        "long_symbol": long[0],
        "long_strike": long[2],
        "long_bid": float(long_quote.get("bp") or 0),
        "long_ask": long_ask,
        "long_quote_ts": long_quote.get("t"),
        "long_delta": (long[3].get("greeks") or {}).get("delta"),
        "long_iv": long[3].get("impliedVolatility"),
        "executable_credit": executable_credit,
        "maximum_loss": (width - executable_credit) * 100,
    }


def option_chain(client: Trader, spot: float, today: dt.date) -> dict:
    snapshots = {}
    token = None
    while True:
        params = {
            "feed": "indicative",
            "type": "put",
            "expiration_date_gte": (today + dt.timedelta(days=30)).isoformat(),
            "expiration_date_lte": (today + dt.timedelta(days=60)).isoformat(),
            "strike_price_gte": round(spot * 0.75, 2),
            "strike_price_lte": round(spot * 0.99, 2),
            "limit": 1000,
        }
        if token:
            params["page_token"] = token
        payload = client._get(client.data_base, "/v1beta1/options/snapshots/SPY", params)
        snapshots.update(payload.get("snapshots") or {})
        token = payload.get("next_page_token")
        if not token:
            return snapshots


def signal_state(client: Trader, today: dt.date) -> dict:
    bars = client.get_bars(["SPY"], today - dt.timedelta(days=430), today).get("SPY")
    if bars is None:
        raise ValueError("SPY history unavailable")
    completed = bars[[stamp.astimezone(ET).date() < today for stamp in bars.index]]
    close = completed["close"]
    if len(close) < 252:
        raise ValueError("insufficient completed SPY history")
    returns = close.pct_change(fill_method=None)
    ma200 = float(close.tail(200).mean())
    vol20 = float(returns.tail(20).std() * np.sqrt(252))
    return {
        "spy_prior_close": float(close.iloc[-1]),
        "spy_ma_200": ma200,
        "realized_vol_20d": vol20,
        "signal_enabled": bool(close.iloc[-1] > ma200 and vol20 < 0.20),
    }


def record(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS observations(
                ts TEXT PRIMARY KEY, profile TEXT, spot REAL, account_equity REAL,
                options_buying_power REAL, signal_enabled INTEGER,
                spy_prior_close REAL, spy_ma_200 REAL, realized_vol_20d REAL,
                expiration_date TEXT, short_symbol TEXT, short_strike REAL,
                short_bid REAL, short_ask REAL, short_delta REAL, short_iv REAL,
                short_quote_ts TEXT, long_symbol TEXT, long_strike REAL,
                long_bid REAL, long_ask REAL, long_delta REAL, long_iv REAL,
                long_quote_ts TEXT, executable_credit REAL, maximum_loss REAL,
                within_risk_budget INTEGER, raw TEXT)
        """)
        conn.execute(
            "INSERT OR REPLACE INTO observations VALUES (" + ",".join("?" * 28) + ")",
            (
                row["ts"], row["profile"], row["spot"], row["account_equity"],
                row["options_buying_power"], int(row["signal_enabled"]),
                row["spy_prior_close"], row["spy_ma_200"], row["realized_vol_20d"],
                row["expiration_date"], row["short_symbol"], row["short_strike"],
                row["short_bid"], row["short_ask"], row["short_delta"], row["short_iv"],
                row["short_quote_ts"], row["long_symbol"], row["long_strike"],
                row["long_bid"], row["long_ask"], row["long_delta"], row["long_iv"],
                row["long_quote_ts"], row["executable_credit"], row["maximum_loss"],
                int(row["within_risk_budget"]), json.dumps(row, sort_keys=True),
            ),
        )


def record_candidate(path: Path, strategy: str, row: dict) -> None:
    """Append a named candidate without changing the legacy control table."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS candidate_observations(
                ts TEXT, strategy TEXT, profile TEXT, spot REAL,
                account_equity REAL, options_buying_power REAL,
                signal_enabled INTEGER, expiration_date TEXT,
                short_symbol TEXT, short_strike REAL, short_delta REAL,
                long_symbol TEXT, long_strike REAL, executable_credit REAL,
                maximum_loss REAL, credit_pct_of_width REAL,
                within_risk_budget INTEGER, credit_qualified INTEGER,
                qualified INTEGER, raw TEXT,
                PRIMARY KEY(ts, strategy))
        """)
        conn.execute(
            "INSERT OR REPLACE INTO candidate_observations VALUES (" +
            ",".join("?" * 20) + ")",
            (
                row["ts"], strategy, row["profile"], row["spot"],
                row["account_equity"], row["options_buying_power"],
                int(row["signal_enabled"]), row["expiration_date"],
                row["short_symbol"], row["short_strike"], row["short_delta"],
                row["long_symbol"], row["long_strike"],
                row["executable_credit"], row["maximum_loss"],
                row["credit_pct_of_width"], int(row["within_risk_budget"]),
                int(row["credit_qualified"]), int(row["qualified"]),
                json.dumps(row, sort_keys=True),
            ),
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=("base", "2x"), default="2x")
    args = parser.parse_args()
    config_name = "config_2x.yaml" if args.profile == "2x" else "config.yaml"
    cfg = load_config(REPO_ROOT / config_name)
    experiments = cfg.sleeves_paper.get("options_experiments", {})
    experiment = experiments.get(
        "bull_put_fixed_width", {}
    )
    if experiment.get("mode", "off") != "shadow":
        print("options shadow off")
        return

    load_env()
    suffix = "_2X" if args.profile == "2x" else ""
    client = Trader(
        key=os.environ.get("ALPACA_API_KEY" + suffix),
        secret=os.environ.get("ALPACA_API_SECRET" + suffix),
    )
    now = dt.datetime.now(ET)
    today = now.date()
    account = client.get_account()
    spot = client.latest_price("SPY")
    if not spot:
        raise ValueError("SPY latest price unavailable")
    signal = signal_state(client, today)
    snapshots = option_chain(client, spot, today)
    pair = select_quote_pair(
        snapshots,
        spot=spot,
        today=today,
        target_dte=int(experiment["target_dte"]),
        short_moneyness=float(experiment["short_moneyness"]),
        width=float(experiment["strike_width"]),
    )
    equity = float(account["equity"])
    row = {
        "ts": now.isoformat(),
        "profile": args.profile,
        "spot": spot,
        "account_equity": equity,
        "options_buying_power": float(account.get("options_buying_power") or 0),
        **signal,
        **pair,
    }
    row["within_risk_budget"] = bool(
        pair["executable_credit"] > 0
        and pair["maximum_loss"] <= equity * float(experiment["max_loss_pct"])
    )
    out = REPO_ROOT / "state" / f"options_shadow_{args.profile}.db"
    record(out, row)
    print(
        f"options shadow: signal={row['signal_enabled']} credit="
        f"${row['executable_credit']:.2f} max_loss=${row['maximum_loss']:.2f} "
        f"within_budget={row['within_risk_budget']}"
    )

    delta_cfg = experiments.get("bull_put_delta_selected", {})
    if delta_cfg.get("mode", "off") == "shadow":
        delta_pair = select_delta_quote_pair(
            snapshots,
            today=today,
            target_dte=int(delta_cfg["target_dte"]),
            target_delta=float(delta_cfg["target_short_delta"]),
            width=float(delta_cfg["strike_width"]),
        )
        candidate = {**row, **delta_pair}
        width = float(delta_cfg["strike_width"])
        candidate["credit_pct_of_width"] = delta_pair["executable_credit"] / width
        candidate["within_risk_budget"] = bool(
            delta_pair["executable_credit"] > 0
            and delta_pair["maximum_loss"] <= equity * float(delta_cfg["max_loss_pct"])
        )
        candidate["credit_qualified"] = bool(
            candidate["credit_pct_of_width"]
            >= float(delta_cfg["min_credit_pct_of_width"])
        )
        candidate["qualified"] = bool(
            candidate["signal_enabled"]
            and candidate["within_risk_budget"]
            and candidate["credit_qualified"]
        )
        record_candidate(out, "bull_put_delta_selected", candidate)
        print(
            f"delta shadow: short_delta={candidate['short_delta']:.3f} "
            f"credit=${candidate['executable_credit']:.2f} "
            f"credit/width={candidate['credit_pct_of_width']:.1%} "
            f"qualified={candidate['qualified']}"
        )


if __name__ == "__main__":
    main()
