# Architecture

## Trust tiers

Three independent services make up the running system. They are not peers —
each has a deliberately different amount of access, and the boundary between
them is enforced by more than a comment:

| Service | Holds broker credentials? | Can mutate state? | Network exposure |
| --- | --- | --- | --- |
| **Engine** (`engine/`, `scripts/`, driven by cron) | Yes | Yes — the only tier that can | none (no inbound port) |
| **Dashboard** (`dashboard/`, port 8787) | No | No — read-only by construction | LAN, no auth |
| **MCP debug server** (`mcp_server/`, port 8788) | No | No — read-only by construction | LAN, no auth |

The engine is the only tier that ever imports `engine.execute` or
`engine.data` (the modules that can reach the Alpaca client) or writes to a
journal. Dashboard and MCP are statically prevented from doing either:
`tests/dashboard/test_safety.py` and `tests/mcp_server/test_mcp_safety.py`
AST-walk both packages and fail the build if they import a broker-capable
module. Both containers additionally have no filesystem access to `.env` —
that's a Docker volume-mount boundary, not just an import check — and mount
`config*.yaml`, `state/`, `reports/`, and `logs/` **read-only**.

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

`scripts/paper.sh` is the single entry point cron calls. Every job flocks a
file under `state/paper-$LOCK.lock` before running, so a slow run can never
overlap the next — but which lock a job takes is a deliberate correctness
decision, not one-lock-per-job:

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

## The ET slot guard

The server clock is UTC and (as of this writing) Debian cron has no
`CRON_TZ`, so every ET-sensitive job needs **two** crontab lines — one that
lands correctly under EDT, one under EST. Both fire year-round; the job's
optional second argument (`scripts/paper.sh daily 09:47`) is the ET time it's
meant to run at, and a firing more than 5 minutes from that slot exits as a
no-op. Exactly one of each pair does real work whatever the DST offset is.
Deleting one line of a pair silently breaks that job for half the year — see
[`AGENTS.md`](../AGENTS.md) for the incident that motivated this guard.
Jobs with no market-clock sensitivity (`weekly`, `iwmfwd`) stay single-line
and unguarded on purpose, since a slot guard would skip them outright for
half the year.

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
