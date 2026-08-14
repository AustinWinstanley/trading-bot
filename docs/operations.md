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

## Deployment (containerized — the current design)

The trading system runs as Docker Compose services (`deploy/docker-compose.yml`):
`engine` (the scheduler + trading logic, via `supercronic` reading
`deploy/crontab`), `journal` (nightly `reports/` commit/push), `dashboard`,
and `mcp-server`. See [docs/architecture.md](architecture.md) for the full
trust-tier and job/lock model.

First-time setup on a server:

```bash
cp deploy/.env.example deploy/.env    # fill in ENGINE_TAG, ENGINE_UID, ENGINE_GID
# .env at the repo root (app credentials) must also exist — see Setup above
mkdir -p deploy/secrets
# place a repo-scoped git deploy key at deploy/secrets/journal_deploy_key
# (write access to this repo only — never a personal key)
docker compose -f deploy/docker-compose.yml pull
docker compose -f deploy/docker-compose.yml up -d
```

Upgrading the engine to a new released version:

```bash
deploy/upgrade.sh v0.2.0
```

`deploy/upgrade.sh` pulls the candidate `engine` image, drains the
`daily`/`daily2x`/`weekly`/`health`/`health2x` locks (the same five
`scripts/upgrade.sh` always drained), then verifies the candidate **before**
switching anything: runs the test suite and both mutation-free dry runs and
both healthchecks *inside the candidate image* via `docker compose run`,
against the real `state/`/`reports/` volumes but without ever starting its
scheduler. Only if every check passes does it `stop` the live `engine`
service and `up -d` it again on the new tag. On any failure, the live
service is untouched — still running the previous tag — and the script
prints the exact rollback command. Dashboard/mcp-server/journal upgrades are
lower-risk (read-only, or narrow-scope) and don't need this battery: bump
their `*_TAG` in `deploy/.env` and run
`docker compose -f deploy/docker-compose.yml up -d`.

Local development (build images from your own tree instead of pulling from
GHCR):

```bash
docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.dev.yml build
docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.dev.yml up
```

Emergency hotfix path if you need to run something the crontab doesn't
schedule: `docker compose run --rm engine python -m scripts.<name> ...` —
this uses the same image, volumes, and credentials as the real scheduled
jobs, just without waiting for a cron slot.

Because `config.yaml`/`config_2x.yaml`/`deploy/crontab` are baked into the
`engine` image rather than mounted, **"the image is what runs" is now
literally true for the trading logic** — an edit only takes effect through
a new image tag and an explicit `deploy/upgrade.sh` run, never a live
directory edit. See [`AGENTS.md`](../AGENTS.md) for what this changes about
how you develop against this repo.

### The journal service and its deploy key

`journal` needs a repo-scoped SSH deploy key (GitHub → repo Settings →
Deploy keys → "Allow write access") placed at
`deploy/secrets/journal_deploy_key` (gitignored, mode 600) before it can
start. It commits nightly (`deploy/journal-crontab`); see
[docs/architecture.md](architecture.md#the-journal-service) for why this is
a separate, narrower trust tier rather than folded into `engine`.

## Legacy host-cron deployment

A server that has not yet migrated to the containerized deployment above
still runs `scripts/paper.sh` directly from the operator's crontab, updated
via:

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
several locks are deliberately shared between jobs, and
[docs/architecture.md's "Legacy host-cron deployment"](architecture.md#legacy-host-cron-deployment)
section for why this mode needs two crontab lines per ET-sensitive job where
the containerized deployment needs only one.

The wrapper uses `flock` to prevent overlapping jobs and `timeout` to bound
stalled runs. Health jobs fail on stale snapshots, inactive accounts, stale
parent orders, or unprotected positions; schedule them separately so a missed
daily job is observable. Deploy only reviewed commits, run the test suite on
the server, then run both dry-run commands before enabling cron.

### Migrating from host cron to containers

`state/paper-*.lock` files are the same bind-mounted inodes either way, so
host cron and the `engine` container can never both run a full rebalance at
the same instant even mid-migration — but running both schedulers
long-term would still double every job. Migrate on a quiet day (no
weekday market-hours jobs pending): stop the crontab's `scripts/paper.sh`
lines first, confirm no `state/paper-*.lock` is held, then bring up the
containerized deployment as described above. Keep the old crontab backup
(`crontab -l > ...`) for a rollback window — restoring it needs
`PAPER_BOT_ROOT` pointed back at the host checkout path, since
`scripts/paper.sh`'s container-oriented default no longer matches a
bare-host layout.
