# trading-bot

Deterministic Alpaca paper-trading system with a pure-Python risk gate,
broker-state reconciliation, and reproducible backtests. No LLM has broker
credentials or participates in sizing or execution.

**Status:** two active paper profiles are configured: an unlevered $4,600
account and a parallel $10,000 2× experiment. Nothing in this repository
enables live trading.

## Money path

```text
cron
  -> engine/portfolio.py     build target weights from completed daily data
  -> scripts/run_daily.py    diff targets against broker positions/orders
  -> engine/risk.py          reject or shrink; never enlarge or invent
  -> engine/execute.py       Alpaca DAY limit + OTO broker stop
  -> state/paper*.db         reconcile journal back to Alpaca order state
```

Alpaca is the source of truth for positions, orders, and fills. Open parent
orders suppress duplicate proposals. Protective orders are canceled before an
intentional exit. Kill-switch liquidation is idempotent and `mode: halt`
flattens without depending on research-data availability.

## Deployed portfolio

The base profile has at most 100% long, 15% short, and 115% gross exposure:

| Sleeve | Exposure | Construction |
| --- | ---: | --- |
| Equity core | 40% long | SPY |
| TSMOM | 25% long/flat | 15 asset ETFs, 12-month trend, inverse volatility |
| Trend | 20% long/flat | SPY above its 200-day average |
| MOM_LS | 15% long + 15% short | Weekly 12-1 momentum, top/bottom 20 |

The 2× profile scales targets to at most 200% long, 30% short, and 230% gross.
It uses separate credentials, state, journal, and reports.

MOM_LS stands down when its weekly target file is absent or stale. Cash is an
intentional residual position.

## Current research estimate

The production-equivalent 2020–2026 backtest includes transaction costs, 3%
short borrow, and 5% margin financing:

| Portfolio | CAGR | Sharpe | Max drawdown |
| --- | ---: | ---: | ---: |
| Base SPY-core profile | 11.90% | 1.057 | -13.30% |
| 2× SPY-core profile | 17.86% | 0.850 | -25.93% |
| SPY buy and hold | 14.99% | 0.883 | -24.50% |

These are estimates, not forecasts. The individual-stock universe contains
currently listed companies and is survivorship-biased. Positive MOM_LS results
are therefore an optimistic upper bound. The sample is also short and dominated
by the post-2020 market.

The former 13F clone sleeve was removed after a timezone-mixed forward-fill
bug was found. Correct point-in-time results reduced its estimated conviction
variant from 27.3% to 5.98% CAGR.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
chmod 600 .env
```

Required environment variables:

- `ALPACA_API_KEY`, `ALPACA_API_SECRET`
- `ALPACA_API_KEY_2X`, `ALPACA_API_SECRET_2X`
- `TIINGO_API_TOKEN` for historical research

All configured Alpaca credentials must point to paper accounts.

## Verification

```bash
.venv/bin/python -m pytest
.venv/bin/python -m backtest.production_portfolio
.venv/bin/python -m backtest.allocation_study
.venv/bin/python -m scripts.run_daily --dry-run --force
.venv/bin/python -m scripts.run_daily --dry-run --force --profile 2x
```

`--dry-run` performs broker reads and writes local journal/report state, but
submits and cancels no orders.

To rebuild the survivorship-biased cross-sectional research matrix:

```bash
.venv/bin/python -m backtest.xsec_data \
  --build-universe \
  --universe state/universe_stocks.json \
  --start 2020-01-01
```

## Deployment

The server wrapper expects the checkout at `/home/austin/trading-bot`:

```bash
scripts/upgrade.sh
```

`upgrade.sh` backs up the user crontab, pauses only `scripts/paper.sh` jobs,
waits for their locks, temporarily stashes tracked server-generated paper
reports, fast-forwards from `origin/main`, installs dependencies, runs tests,
executes both mutation-free dry runs, and checks both paper accounts. It runs
from a temporary copy so pulling cannot modify the executing script. It
restores the report stash on every exit and restores the exact original
crontab only on success. If any check fails, trading cron remains paused and
the script prints the one-line cron recovery command using its timestamped
backup in `state/`.

Individual wrapper jobs remain available:

```bash
scripts/paper.sh daily
scripts/paper.sh daily2x
scripts/paper.sh weekly
scripts/paper.sh health
scripts/paper.sh health2x
```

The wrapper uses `flock` to prevent overlapping jobs and `timeout` to bound
stalled runs. Health jobs fail on stale snapshots, inactive accounts, stale
parent orders, or unprotected positions; schedule them separately so a missed
daily job is observable. Deploy only reviewed commits, run the test suite on
the server, then run both dry-run commands before enabling cron.

## Safety contract

`engine/risk.py` may reject or shrink a proposal. It may never enlarge one or
invent a symbol. Config validation refuses averaging down, non-limit entries,
shorting without an explicit cap, and inconsistent exposure limits.

Risk controls include:

- single-name, long, short, gross, and leveraged-ETF exposure caps;
- daily, monthly, and peak-drawdown circuit breakers;
- broker-held OTO stops plus a local monitoring fallback;
- no averaging down;
- loss re-entry cooldown;
- liquidity, price, IPO-age, borrowability, and slippage filters;
- opening/closing entry windows.

This is experimental software, not investment advice.
