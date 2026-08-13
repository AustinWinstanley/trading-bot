"""One-off, read-only check: does this Alpaca paper account have options
trading approved at the level multi-leg spreads require?

Alpaca requires options trading Level 3 for multi-leg spread strategies
(vertical spreads, butterflies, iron condors) — nothing in this repo has
ever verified the 2x paper account actually has it, since every existing
options script only reads quote/chain data, never submits an order. Run
this by hand before any experiment in config_2x.yaml's `experiments:`
block is flipped to a real options structure's `status: paper` — a level
below 3 means Alpaca will reject every multi-leg order regardless of how
correct the rest of the code is.

This same check also runs inline, every run, inside scripts/options_daily.py
(defense in depth, not a one-time gate) — this script is for a human to run
once, by hand, first.

    python -m scripts.check_options_level --profile 2x

Read-only: only GET /v2/account. Nothing here submits, cancels, or
otherwise mutates a broker account.
"""

from __future__ import annotations

import argparse
import os

from engine.data import load_env
from engine.execute import Trader

MIN_LEVEL = 3


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=("base", "2x"), default="2x")
    args = parser.parse_args()

    load_env()
    suffix = "_2X" if args.profile == "2x" else ""
    key = os.environ.get(f"ALPACA_API_KEY{suffix}")
    secret = os.environ.get(f"ALPACA_API_SECRET{suffix}")
    if suffix and not (key and secret):
        print(f"CRITICAL: profile {args.profile} needs ALPACA_API_KEY{suffix} / "
              f"ALPACA_API_SECRET{suffix} in .env — standing down")
        raise SystemExit(1)
    trader = Trader(key=key, secret=secret) if suffix else Trader()

    account = trader.get_account()
    approved = account.get("options_approved_level")
    trading = account.get("options_trading_level")
    buying_power = account.get("options_buying_power")
    print(f"profile={args.profile} options_approved_level={approved} "
          f"options_trading_level={trading} options_buying_power={buying_power}")

    level = trading if trading is not None else approved
    if level is None or int(level) < MIN_LEVEL:
        print(
            f"CRITICAL: options level {level!r} is below the Level {MIN_LEVEL} "
            "multi-leg spreads require (or the field is absent) — Alpaca will "
            "reject every mleg order; do not flip any options experiment to "
            "status: paper until this is resolved"
        )
        raise SystemExit(1)
    print(f"HEALTHY: options level {level} supports multi-leg spreads")


if __name__ == "__main__":
    main()
