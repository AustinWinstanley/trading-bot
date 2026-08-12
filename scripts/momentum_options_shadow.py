"""Read-only option-vertical observations for weekly MOM_LS ranks.

The strongest ranked long is represented by a bull call debit spread and the
weakest ranked short by a bear put debit spread. Nothing in this module has a
broker mutation method; it records quote-side feasibility only.
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
OCC = re.compile(
    r"^(?P<root>[A-Z]+)(?P<expiry>\d{6})(?P<kind>[CP])(?P<strike>\d{8})$"
)

# Belt-and-suspenders against an API that never advances (or echoes a
# stable) page token — bounds worst-case pagination regardless of server
# behavior.
MAX_PAGES = 20


def run_slot(now: dt.datetime) -> str:
    """Coarse tag for which of shadows2x's two daily firings produced a row —
    see scripts/options_shadow.py's run_slot for the full rationale."""
    return "morning" if now.astimezone(ET).time() < dt.time(11, 30) else "midday"


def parse_option(symbol: str) -> tuple[str, dt.date, str, float] | None:
    match = OCC.match(symbol)
    if not match:
        return None
    return (
        match.group("root"),
        dt.datetime.strptime(match.group("expiry"), "%y%m%d").date(),
        "call" if match.group("kind") == "C" else "put",
        int(match.group("strike")) / 1000,
    )


def option_chain(
    client: Trader,
    symbol: str,
    *,
    spot: float,
    today: dt.date,
    kind: str,
) -> dict:
    snapshots = {}
    token = None
    for _ in range(MAX_PAGES):
        params = {
            "feed": "indicative",
            "type": kind,
            "expiration_date_gte": (today + dt.timedelta(days=35)).isoformat(),
            "expiration_date_lte": (today + dt.timedelta(days=75)).isoformat(),
            "strike_price_gte": round(spot * (0.65 if kind == "put" else 0.75), 2),
            "strike_price_lte": round(spot * (1.15 if kind == "put" else 1.35), 2),
            "limit": 1000,
        }
        if token:
            params["page_token"] = token
        payload = client._get(
            client.data_base, f"/v1beta1/options/snapshots/{symbol}", params
        )
        snapshots.update(payload.get("snapshots") or {})
        next_token = payload.get("next_page_token")
        if not next_token or next_token == token:
            return snapshots
        token = next_token
    return snapshots


def select_directional_vertical(
    snapshots: dict,
    *,
    underlying: str,
    direction: str,
    today: dt.date,
    target_dte: int,
    long_delta: float,
    short_delta: float,
) -> dict:
    if direction not in {"bull_call", "bear_put"}:
        raise ValueError(f"unknown direction {direction!r}")
    expected_kind = "call" if direction == "bull_call" else "put"
    rows = []
    for symbol, snapshot in snapshots.items():
        parsed = parse_option(symbol)
        delta = (snapshot.get("greeks") or {}).get("delta")
        if not parsed or delta is None or not np.isfinite(float(delta)):
            continue
        root, expiry, kind, strike = parsed
        if root != underlying or kind != expected_kind:
            continue
        if expiry.weekday() != 4 or not 15 <= expiry.day <= 21:
            continue
        quote = snapshot.get("latestQuote") or {}
        if not all(float(quote.get(k) or 0) > 0 for k in ("bp", "ap", "bs", "as")):
            continue
        rows.append((symbol, expiry, strike, snapshot, abs(float(delta))))
    if not rows:
        raise ValueError("no liquid standard-monthly contracts with Greeks")
    expiry = min({r[1] for r in rows}, key=lambda d: abs((d - today).days - target_dte))
    same_expiry = [r for r in rows if r[1] == expiry]
    long_leg = min(same_expiry, key=lambda r: abs(r[4] - long_delta))
    short_candidates = [
        r for r in same_expiry
        if (r[2] > long_leg[2] if direction == "bull_call" else r[2] < long_leg[2])
    ]
    if not short_candidates:
        raise ValueError("no correctly ordered short leg")
    short_leg = min(short_candidates, key=lambda r: abs(r[4] - short_delta))
    long_quote = long_leg[3]["latestQuote"]
    short_quote = short_leg[3]["latestQuote"]
    debit = float(long_quote["ap"]) - float(short_quote["bp"])
    width = abs(short_leg[2] - long_leg[2])
    maximum_profit = (width - debit) * 100
    maximum_loss = max(debit, 0) * 100
    reward_to_risk = maximum_profit / maximum_loss if maximum_loss > 0 else 0.0
    return {
        "underlying": underlying,
        "direction": direction,
        "expiration_date": expiry.isoformat(),
        "long_symbol": long_leg[0],
        "long_strike": long_leg[2],
        "long_delta": (long_leg[3].get("greeks") or {}).get("delta"),
        "long_ask": float(long_quote["ap"]),
        "long_quote_ts": long_quote.get("t"),
        "short_symbol": short_leg[0],
        "short_strike": short_leg[2],
        "short_delta": (short_leg[3].get("greeks") or {}).get("delta"),
        "short_bid": float(short_quote["bp"]),
        "short_quote_ts": short_quote.get("t"),
        "width": width,
        "net_debit": debit,
        "maximum_profit": maximum_profit,
        "maximum_loss": maximum_loss,
        "reward_to_risk": reward_to_risk,
    }


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
                ts TEXT, profile TEXT, rank_side TEXT, rank INTEGER,
                underlying TEXT, direction TEXT, expiration_date TEXT,
                long_symbol TEXT, long_strike REAL, long_delta REAL,
                short_symbol TEXT, short_strike REAL, short_delta REAL,
                net_debit REAL, maximum_profit REAL, maximum_loss REAL,
                reward_to_risk REAL, qualified INTEGER, reason TEXT, raw TEXT,
                PRIMARY KEY(ts, rank_side))
        """)
        _ensure_column(conn, "observations", "run_slot", "TEXT")
        conn.execute(
            "INSERT OR REPLACE INTO observations "
            "(ts, profile, rank_side, rank, underlying, direction, "
            "expiration_date, long_symbol, long_strike, long_delta, "
            "short_symbol, short_strike, short_delta, net_debit, "
            "maximum_profit, maximum_loss, reward_to_risk, qualified, "
            "reason, raw, run_slot) VALUES (" + ",".join("?" * 21) + ")",
            (
                row["ts"], row["profile"], row["rank_side"], row["rank"],
                row["underlying"], row["direction"], row["expiration_date"],
                row["long_symbol"], row["long_strike"], row["long_delta"],
                row["short_symbol"], row["short_strike"], row["short_delta"],
                row["net_debit"], row["maximum_profit"], row["maximum_loss"],
                row["reward_to_risk"], int(row["qualified"]), row["reason"],
                json.dumps(row, sort_keys=True), row["run_slot"],
            ),
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=("base", "2x"), default="2x")
    args = parser.parse_args()
    config_name = "config_2x.yaml" if args.profile == "2x" else "config.yaml"
    cfg = load_config(REPO_ROOT / config_name)
    settings = cfg.sleeves_paper.get("options_experiments", {}).get(
        "momentum_verticals", {}
    )
    if settings.get("mode", "off") != "shadow":
        print("momentum options shadow off")
        return
    targets_path = REPO_ROOT / cfg.sleeves_paper["mom_ls_targets_file"]
    if not targets_path.exists():
        print("momentum options shadow: weekly target file absent; standing down")
        return
    targets = json.loads(targets_path.read_text())
    today = dt.datetime.now(ET).date()
    age = (today - dt.date.fromisoformat(targets["as_of"])).days
    if age > int(cfg.sleeves_paper["mom_ls_max_age_days"]):
        print("momentum options shadow: weekly targets stale; standing down")
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
    account = client.get_account()
    equity = float(account["equity"])
    now_dt = dt.datetime.now(ET)
    now = now_dt.isoformat()
    output = REPO_ROOT / "state" / f"momentum_options_shadow_{args.profile}.db"
    attempts = int(settings["max_rank_attempts_per_side"])
    for rank_side, symbols, direction, kind in (
        ("long", targets.get("long", []), "bull_call", "call"),
        ("short", targets.get("short", []), "bear_put", "put"),
    ):
        selected = None
        errors = []
        for rank, symbol in enumerate(symbols[:attempts], start=1):
            try:
                spot = client.latest_price(symbol)
                if not spot:
                    raise ValueError("latest price unavailable")
                selected = select_directional_vertical(
                    option_chain(client, symbol, spot=spot, today=today, kind=kind),
                    underlying=symbol,
                    direction=direction,
                    today=today,
                    target_dte=int(settings["target_dte"]),
                    long_delta=float(settings["long_delta"]),
                    short_delta=float(settings["short_delta"]),
                )
                selected.update(rank=rank, rank_side=rank_side)
                break
            except Exception as exc:
                errors.append(f"{symbol}: {exc}")
        if selected is None:
            print(f"momentum options {rank_side}: no candidate ({'; '.join(errors)})")
            continue
        selected.update(ts=now, profile=args.profile, run_slot=run_slot(now_dt))
        selected["qualified"] = bool(
            0 < selected["maximum_loss"]
            <= equity * float(settings["max_debit_pct"])
            and selected["reward_to_risk"] >= float(settings["min_reward_to_risk"])
        )
        selected["reason"] = (
            "qualified" if selected["qualified"] else
            "debit or reward/risk outside pre-registered bounds"
        )
        record(output, selected)
        print(
            f"momentum options {rank_side}: {selected['underlying']} rank "
            f"{selected['rank']} debit=${selected['net_debit']:.2f} "
            f"max_loss=${selected['maximum_loss']:.0f} "
            f"reward/risk={selected['reward_to_risk']:.2f} "
            f"qualified={selected['qualified']}"
        )


if __name__ == "__main__":
    main()
