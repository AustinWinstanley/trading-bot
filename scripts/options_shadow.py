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

# Matches backtest/bull_put_credit_spread_study.py's FRICTION_PER_LEG and
# round_trip_friction (4 legs: open+close on both the short and long leg).
# The shadow's whole purpose is comparing live-quote feasibility against the
# study's verdict; a shadow that omits the cost the study charges "qualifies"
# trades the study would have rejected as over budget — see the recorded
# 2026-08-12 observation, which read within_risk_budget=True at $492 when
# the study's own formula puts it at $532, over the $500 budget.
FRICTION_PER_LEG = 0.10
ROUND_TRIP_FRICTION = 4 * FRICTION_PER_LEG

# Belt-and-suspenders against an API that never advances (or echoes a
# stable) page token — bounds worst-case pagination regardless of server
# behavior. 20 pages * 1000 contracts/page comfortably covers any real
# SPY chain in the strike/expiry windows these selectors query.
MAX_PAGES = 20


def run_slot(now: dt.datetime) -> str:
    """Coarse tag for which of shadows2x's two daily firings produced a row.

    A 0DTE/short-dated structure's risk profile differs meaningfully between
    the morning and midday runs (different time-to-expiry, different
    intraday vol regime); without this tag, review minima stated in
    observation counts silently mix two regimes into one number. The cutoff
    sits at the midpoint between the two scheduled shadows2x firings
    (~10:06 and ~12:54 ET).
    """
    return "morning" if now.astimezone(ET).time() < dt.time(11, 30) else "midday"


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
        quote = snapshot.get("latestQuote") or {}
        if not all(float(quote.get(k) or 0) > 0 for k in ("bp", "ap", "bs", "as")):
            continue  # no displayed size on this leg's quote — not executable
        rows.append((symbol, expiry, strike, snapshot))
    if not rows:
        raise ValueError("no standard-monthly SPY put snapshots with displayed size")
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
    maximum_loss = (width - executable_credit + ROUND_TRIP_FRICTION) * 100
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
        quote = snapshot.get("latestQuote") or {}
        if not all(float(quote.get(k) or 0) > 0 for k in ("bp", "ap", "bs", "as")):
            continue  # no displayed size on this leg's quote — not executable
        rows.append((symbol, expiry, strike, snapshot))
    if not rows:
        raise ValueError("no standard-monthly SPY puts with Greeks and displayed size")
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
        "maximum_loss": (width - executable_credit + ROUND_TRIP_FRICTION) * 100,
    }


def select_put_broken_wing(
    snapshots: dict,
    *,
    today: dt.date,
    target_dte: int,
    target_upper_delta: float,
    upper_width: float,
    lower_width: float,
) -> dict:
    """Select a 1/-2/1 put butterfly and price it at executable quotes."""
    rows = []
    for symbol, snapshot in snapshots.items():
        parsed = parse_contract(symbol)
        delta = (snapshot.get("greeks") or {}).get("delta")
        if not parsed or delta is None or not np.isfinite(float(delta)):
            continue
        expiry, strike = parsed
        if expiry.weekday() != 4 or not 15 <= expiry.day <= 21:
            continue
        quote = snapshot.get("latestQuote") or {}
        if not all(float(quote.get(k) or 0) > 0 for k in ("bp", "ap", "bs", "as")):
            continue  # no displayed size on this leg's quote — not executable
        rows.append((symbol, expiry, strike, snapshot))
    if not rows:
        raise ValueError("no standard-monthly puts with displayed size for broken-wing butterfly")
    expiry = min({r[1] for r in rows}, key=lambda d: abs((d - today).days - target_dte))
    same_expiry = [r for r in rows if r[1] == expiry]
    structures = []
    for upper in same_expiry:
        middle = [r for r in same_expiry if np.isclose(r[2], upper[2] - upper_width)]
        if not middle:
            continue
        lower_strike = middle[0][2] - lower_width
        lower = [r for r in same_expiry if np.isclose(r[2], lower_strike)]
        if lower:
            delta = abs(float((upper[3].get("greeks") or {})["delta"]))
            structures.append((abs(delta - target_upper_delta), upper, middle[0], lower[0]))
    if not structures:
        raise ValueError("no exact broken-wing strike geometry")
    _, upper, middle, lower = min(structures, key=lambda row: row[0])

    def quote(row):
        return row[3].get("latestQuote") or {}

    upper_ask = float(quote(upper).get("ap") or 0)
    middle_bid = float(quote(middle).get("bp") or 0)
    lower_ask = float(quote(lower).get("ap") or 0)
    net_debit = upper_ask + lower_ask - 2 * middle_bid
    peak_profit = (upper_width - net_debit) * 100
    downside_loss = (lower_width - upper_width + net_debit) * 100
    # Unlike select_quote_pair/select_delta_quote_pair above, this does NOT
    # add ROUND_TRIP_FRICTION: no backtest study establishes a friction
    # convention for this 4-leg structure (the $0.40 figure comes from
    # backtest/bull_put_credit_spread_study.py's 2-leg spread), and inventing
    # one here would be no more justified than omitting it. Treat this
    # max_loss as a lower bound until a butterfly study exists.
    maximum_loss = max(net_debit * 100, downside_loss, 0.0)
    return {
        "expiration_date": expiry.isoformat(),
        "short_symbol": middle[0],
        "short_strike": middle[2],
        "short_bid": middle_bid,
        "short_ask": float(quote(middle).get("ap") or 0),
        "short_quote_ts": quote(middle).get("t"),
        "short_delta": (middle[3].get("greeks") or {}).get("delta"),
        "short_iv": middle[3].get("impliedVolatility"),
        "long_symbol": upper[0],
        "long_strike": upper[2],
        "long_bid": float(quote(upper).get("bp") or 0),
        "long_ask": upper_ask,
        "long_quote_ts": quote(upper).get("t"),
        "long_delta": (upper[3].get("greeks") or {}).get("delta"),
        "long_iv": upper[3].get("impliedVolatility"),
        "lower_long_symbol": lower[0],
        "lower_long_strike": lower[2],
        "lower_long_bid": float(quote(lower).get("bp") or 0),
        "lower_long_ask": lower_ask,
        "lower_long_delta": (lower[3].get("greeks") or {}).get("delta"),
        "net_debit": net_debit,
        # Generic candidate columns use credit; a debit is negative credit.
        "executable_credit": -net_debit,
        "maximum_loss": maximum_loss,
        "peak_profit": peak_profit,
    }


def option_chain(client: Trader, spot: float, today: dt.date) -> dict:
    snapshots = {}
    token = None
    for _ in range(MAX_PAGES):
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
        next_token = payload.get("next_page_token")
        if not next_token or next_token == token:
            return snapshots
        token = next_token
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


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, kind: str) -> None:
    """Additive migration so an already-deployed table gains new columns
    without losing rows — CREATE TABLE IF NOT EXISTS alone does nothing for
    a table that already exists with an older schema. Mirrors
    scripts/run_daily.py's db() migration pattern."""
    existing = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")


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
        _ensure_column(conn, "observations", "run_slot", "TEXT")
        conn.execute(
            "INSERT OR REPLACE INTO observations "
            "(ts, profile, spot, account_equity, options_buying_power, "
            "signal_enabled, spy_prior_close, spy_ma_200, realized_vol_20d, "
            "expiration_date, short_symbol, short_strike, short_bid, short_ask, "
            "short_delta, short_iv, short_quote_ts, long_symbol, long_strike, "
            "long_bid, long_ask, long_delta, long_iv, long_quote_ts, "
            "executable_credit, maximum_loss, within_risk_budget, raw, run_slot) "
            "VALUES (" + ",".join("?" * 29) + ")",
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
                row["run_slot"],
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
        _ensure_column(conn, "candidate_observations", "run_slot", "TEXT")
        conn.execute(
            "INSERT OR REPLACE INTO candidate_observations "
            "(ts, strategy, profile, spot, account_equity, options_buying_power, "
            "signal_enabled, expiration_date, short_symbol, short_strike, "
            "short_delta, long_symbol, long_strike, executable_credit, "
            "maximum_loss, credit_pct_of_width, within_risk_budget, "
            "credit_qualified, qualified, raw, run_slot) "
            "VALUES (" + ",".join("?" * 21) + ")",
            (
                row["ts"], strategy, row["profile"], row["spot"],
                row["account_equity"], row["options_buying_power"],
                int(row["signal_enabled"]), row["expiration_date"],
                row["short_symbol"], row["short_strike"], row["short_delta"],
                row["long_symbol"], row["long_strike"],
                row["executable_credit"], row["maximum_loss"],
                row["credit_pct_of_width"], int(row["within_risk_budget"]),
                int(row["credit_qualified"]), int(row["qualified"]),
                json.dumps(row, sort_keys=True), row["run_slot"],
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
        "run_slot": run_slot(now),
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

    butterfly_cfg = experiments.get("put_broken_wing_butterfly", {})
    if butterfly_cfg.get("mode", "off") == "shadow":
        butterfly_pair = select_put_broken_wing(
            snapshots,
            today=today,
            target_dte=int(butterfly_cfg["target_dte"]),
            target_upper_delta=float(butterfly_cfg["target_upper_delta"]),
            upper_width=float(butterfly_cfg["upper_width"]),
            lower_width=float(butterfly_cfg["lower_width"]),
        )
        butterfly = {**row, **butterfly_pair}
        # Retain the common schema; for a debit structure this expresses the
        # signed net credit relative to the upper profit wing.
        butterfly["credit_pct_of_width"] = (
            butterfly["executable_credit"] / float(butterfly_cfg["upper_width"])
        )
        butterfly["signal_enabled"] = bool(
            butterfly["spy_prior_close"] > butterfly["spy_ma_200"]
            and butterfly["realized_vol_20d"] < float(butterfly_cfg["max_realized_vol"])
        )
        butterfly["within_risk_budget"] = bool(
            butterfly["maximum_loss"]
            <= equity * float(butterfly_cfg["max_loss_pct"])
        )
        butterfly["credit_qualified"] = bool(
            butterfly["net_debit"] <= float(butterfly_cfg["max_entry_debit"])
            and butterfly["peak_profit"]
            >= float(butterfly_cfg["min_peak_profit_dollars"])
        )
        butterfly["qualified"] = bool(
            butterfly["signal_enabled"]
            and butterfly["within_risk_budget"]
            and butterfly["credit_qualified"]
        )
        record_candidate(out, "put_broken_wing_butterfly", butterfly)
        print(
            f"butterfly shadow: upper_delta={butterfly['long_delta']:.3f} "
            f"debit=${butterfly['net_debit']:.2f} "
            f"peak=${butterfly['peak_profit']:.0f} "
            f"max_loss=${butterfly['maximum_loss']:.0f} "
            f"qualified={butterfly['qualified']}"
        )


if __name__ == "__main__":
    main()
