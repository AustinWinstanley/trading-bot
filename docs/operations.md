# Operations

Setup, verification, and deployment for running this system yourself. See
[`AGENTS.md`](../AGENTS.md) for the conventions that keep a live change
safe — this page is the mechanics.

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
- `SEC_USER_AGENT` for any SEC EDGAR feature (insider transactions, 13F,
  fundamentals) — see [docs/data.md](data.md)

**All configured Alpaca credentials must point to paper accounts.** Nothing
in this repository is configured or intended for live trading.

## Verification

```bash
.venv/bin/python -m pytest
.venv/bin/python -m backtest.production_portfolio
.venv/bin/python -m backtest.allocation_study
.venv/bin/python -m backtest.capital_split_study
.venv/bin/python -m backtest.defensive_rotation_study
.venv/bin/python -m backtest.momentum_sleeve_overlay_study
.venv/bin/python -m backtest.pilot_follower_study
.venv/bin/python -m backtest.options_strategy_study
.venv/bin/python -m backtest.spy_put_spread_study
.venv/bin/python -m backtest.anticipatory_tail_hedge_study
.venv/bin/python -m backtest.bull_call_spread_study
.venv/bin/python -m scripts.run_daily --dry-run --force
.venv/bin/python -m scripts.run_daily --dry-run --force --profile 2x
```

`--dry-run` performs broker reads but submits no orders and rolls back all
local journal/report state.

To rebuild the survivorship-biased cross-sectional research matrix:

```bash
.venv/bin/python -m backtest.xsec_data \
  --build-universe \
  --universe state/universe_stocks.json \
  --start 2020-01-01
```

See [docs/data.md](data.md) for rebuilding the Tiingo/EDGAR-derived caches
this repo no longer tracks in git.

## Deployment

Run the upgrade wrapper from the repository checkout on the server:

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
backup in `state/`. During an upgrade only, a completely unused profile with
an active account, no positions, no orders, and an empty journal may pass
without a snapshot; the regular scheduled health check remains strict until
the profile completes its first live run.

Individual wrapper jobs remain available:

```bash
scripts/paper.sh daily 09:47      # second argument is the intended ET slot
scripts/paper.sh daily2x 09:51
scripts/paper.sh weekly           # omit the slot to run unconditionally
scripts/paper.sh health
scripts/paper.sh health2x
```

See [docs/architecture.md](architecture.md) for the full job table and why
several locks are deliberately shared between jobs.

### The ET slot guard

The server clock is UTC and Debian cron has no `CRON_TZ`, so every ET slot
needs **two** crontab lines — one correct under EDT (UTC−4), one under EST
(UTC−5). Both fire year round. The optional second argument is the ET time the
job is meant to run at; a firing more than 5 minutes away from it exits as a
no-op, so exactly one of each pair does work whatever the offset.

Before this guard both lines of every pair ran, so each job executed **four**
times a weekday, and the out-of-season copy did not merely duplicate the run —
it traded an hour late. **Do not delete either line of a pair**, and give any
new market-hours job a slot argument. Jobs with no market-clock sensitivity
(the Sunday refresh) stay single-line and unguarded, because a slot guard would
skip them outright for half the year.

The wrapper uses `flock` to prevent overlapping jobs and `timeout` to bound
stalled runs. Health jobs fail on stale snapshots, inactive accounts, stale
parent orders, or unprotected positions; schedule them separately so a missed
daily job is observable. Deploy only reviewed commits, run the test suite on
the server, then run both dry-run commands before enabling cron.
