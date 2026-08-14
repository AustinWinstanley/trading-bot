# Architecture

Everything in this repo runs as one of four Docker Compose services
(`deploy/docker-compose.yml`). A server that hasn't yet cut over from the
legacy host-cron deployment still runs `scripts/paper.sh` directly from
cron for the trading logic (see "Legacy host-cron deployment" below) — the
trust-tier model is identical either way, only the scheduler differs.

## Trust tiers

Four independent services make up the running system. They are not peers —
each has a deliberately different amount of access, and the boundary between
them is enforced by more than a comment:

| Service | Holds broker credentials? | Can mutate state? | Network exposure |
| --- | --- | --- | --- |
| **`engine`** (`engine/`, `scripts/`, `backtest/`) | Yes, via `env_file` | Yes — the only tier that can | none (no inbound port) |
| **`journal`** | No | Only `.git` in the mounted checkout, via a repo-scoped deploy key | none (no inbound port) |
| **`dashboard`** (port 8787) | No | No — read-only by construction | LAN, no auth |
| **`mcp-server`** (port 8788) | No | No — read-only by construction | LAN, no auth |

`engine` is the only tier that ever imports `engine.execute` or
`engine.data` (the modules that can reach the Alpaca client) or writes to a
trading journal. `dashboard` and `mcp-server` are statically prevented from
doing either: `tests/dashboard/test_safety.py` and
`tests/mcp_server/test_mcp_safety.py` AST-walk both packages and fail the
build if they import a broker-capable module;
`tests/test_compose_invariants.py` checks the same boundary at the compose
level — neither service may carry an `env_file`, an inline `environment:`
block, or a read-write volume mount. Both containers additionally have no
filesystem access to `.env` at all — that's a Docker volume-mount boundary,
not just an import check — and mount `config*.yaml`, `state/`, `reports/`,
and `logs/` **read-only**.

`journal` is a third, narrower tier of its own: it holds a repo-scoped git
deploy key (write access to this repo only) and nothing else — no broker
credentials, no `.env`. It exists because `reports/` stays git-tracked and
public even though `engine` now deploys as a versioned image rather than a
git pull; see "The journal service" below.

`mcp_server`'s ad hoc SQL tool (`query_database`) adds one more layer:
SQLite connections are opened `mode=ro`, the query is checked to be
`SELECT`/`WITH` only, and `sqlite3.Cursor.execute()` itself refuses to run
more than one statement — three independent guards on the same guarantee.
File-read tools (`read_state_file`, `read_report`, `tail_trading_log`) are
path-traversal-guarded to stay under their respective directories.

Both dashboard and MCP ship with **zero authentication**, deliberately —
see [`SECURITY.md`](../SECURITY.md) for what that does and doesn't mean for
your own deployment.

## The daily job set and why locks are shared, not per-job

`scripts/paper.sh` is the single entry point the scheduler calls — inside
the `engine` container that's `supercronic` reading `deploy/crontab`; on a
not-yet-cut-over host it's cron reading the operator's crontab. Either way,
every job flocks a file under `state/paper-$LOCK.lock` before running, so a
slow run can never overlap the next — and because `state/` is the same
bind-mounted directory in both cases, flock's exclusion holds correctly
even during the transition itself (a host cron job and a containerized job
racing the same lock file still can't both proceed). Which lock a job takes
is a deliberate correctness decision, not one-lock-per-job:

| Job | Lock | What it does |
| --- | --- | --- |
| `daily` | `daily` | Full base-profile rebalance |
| `daily2x` | `daily2x` | Full 2×-profile rebalance |
| `stops` | `daily` (shared) | Base stop-only check |
| `stops2x` | `daily2x` (shared) | 2× stop-only check |
| `options_daily2x` | `daily2x` (shared) | 2× options-experiment order submission |
| `shadows2x` | `shadows2x` (own) | Four read-only 2× research collectors |
| `weekly` | `weekly` | Full-universe weekly rebuild (base + 2×) |
| `momls2x` | `weekly` (shared) | Mid-week 2×-only MOM_LS rebuild |
| `iwmfwd` | `iwmfwd` (own) | Read-only IWM breakout forward-test recorder |
| `health` | `health` | Base health check |
| `health2x` | `health2x` | 2× health check |

The sharing is load-bearing, not incidental:

- **`stops`/`stops2x` share `daily`/`daily2x`'s lock** because a stops check
  must never run concurrently with a full rebalance against the same SQLite
  journal. A stops check that loses the race simply waits for the next cron
  minute — the full run checks stops itself in the same cycle, so nothing is
  missed.
- **`options_daily2x` shares `daily2x`'s lock**, not its own, because it
  writes real orders and read-modify-writes `state/risk_state_2x.json`'s
  `experiment_realized_pnl`/`experiment_standdowns` keys — the exact keys
  `scripts.run_daily --profile 2x` also owns. An independent lock would let
  the two race that file and submit orders to the same account concurrently.
- **`shadows2x` and `iwmfwd` get their own locks**, precisely *because* they
  are read-only research collectors. Until 2026-08-12 `shadows2x` shared
  `daily2x`'s lock; a slow quote fetch could then starve a `stops2x` check
  via `flock -n` — research latency borrowing against a live risk control's
  responsiveness. Splitting the lock fixed that class of problem for good.
- **`momls2x` shares `weekly`'s lock** because both write
  `state/mom_ls_targets_2x.json` and must never race each other.

## Scheduling: supercronic + CRON_TZ, and the ET slot guard's new role

`deploy/crontab` (baked into the `engine` image, read by `supercronic`) sets
`CRON_TZ=America/New_York` once at the top of the file — every schedule line
below it is interpreted in US Eastern time, DST included, via Go's IANA
tzdata. This makes each ET-sensitive job **one** crontab line, not two:
`supercronic` handles the EDT/EST transition itself, unlike Debian host
cron, which has no `CRON_TZ` and forced the old two-line-per-slot
convention (see `tests/test_deploy_crontab.py` for the checks that keep
this file correct — CRON_TZ present, no duplicate job/slot pairs, every
slotted job's cron fields matching its own slot argument).

`scripts/paper.sh`'s ET slot guard — the optional second argument
(`scripts/paper.sh daily 09:47`) that makes a firing more than 5 minutes
from that slot exit as a no-op — still runs on every slotted job. Under
`CRON_TZ` it should never actually trigger, since there's no second,
wrong-half-of-the-year line to produce a mismatched firing; it stays in
place as a belt-and-suspenders check, so a future TZ misconfiguration fails
as a loud, logged skip instead of a silent wrong-time trade. Jobs with no
market-clock sensitivity (`weekly`, `momls2x`, `shadows2x`, `iwmfwd`) stay
unslotted on purpose.

### Legacy host-cron deployment

A server that has not yet cut over to `deploy/docker-compose.yml` still
runs `scripts/paper.sh` directly from the operator's crontab, which — on
Debian, with no `CRON_TZ` support — needs the original **two** lines per
ET-sensitive slot: one that lands correctly under EDT (UTC−4), one under
EST (UTC−5). Both fire year-round; the slot guard is what makes exactly one
of each pair do real work whatever the DST offset is. Deleting one line of
a pair silently breaks that job for half the year — see
[`AGENTS.md`](../AGENTS.md) for the incident that motivated this guard in
the first place. See [docs/operations.md](operations.md) for which
deployment mode applies to a given server and how to move from one to the
other.

## The journal service

`reports/` stays git-tracked and public — this repo's paper-trading record
— even though `engine` now deploys as a versioned image rather than a git
pull, so nothing keeps the server's checkout in sync with `origin/main` the
way `git pull` inside the old host-cron `scripts/upgrade.sh` used to.
`journal` (`deploy/journal.Dockerfile`, a separate scheduled `supercronic`
job of its own — see `deploy/journal-crontab`) fills that gap nightly:
`git pull --ff-only` (never a merge — a real divergence is a human problem,
not something to paper over) delivers dev-committed research, including
`reports/experiments/*.json` registrations `engine/config.py` validates
exist at startup, into the same checkout `engine` mounts read-write; then
`git add reports/paper*/ && git commit && git push` publishes whatever
`engine` wrote that day. It holds a repo-scoped SSH deploy key — write
access to this repo only — mounted via Compose `secrets:`, never `.env`,
and `engine` itself has no git credential at all.

## Money path

```text
cron
  -> engine/portfolio.py     build target weights from completed daily data
  -> scripts/run_daily.py    diff targets against broker positions/orders
  -> engine/risk.py          reject or shrink; never enlarge or invent
  -> engine/execute.py       Alpaca DAY limit + whole-share OTO broker stop
  -> state/paper*.db         reconcile journal back to Alpaca order state
```

Alpaca is the source of truth for positions, orders, and fills. Open parent
orders suppress duplicate proposals. Protective orders are canceled before an
intentional exit. Kill-switch liquidation is idempotent and `mode: halt`
flattens without depending on research-data availability.

`engine/risk.py`'s gate is the single most important file in this
architecture: it may reject or shrink a proposal, and — enforced by an
in-process runtime assertion, not just review — it may never enlarge one or
invent a symbol. See [`AGENTS.md`](../AGENTS.md#the-risk-gate-is-the-most-important-code-here)
for the full contract and [`SECURITY.md`](../SECURITY.md) for how that
invariant fits the overall trust model.
