# Working on this repo

Operating notes for anyone — human or agent — picking this up cold. `README.md`
describes what the system *is*; this describes what will mislead you about it.

## This is live, and the working tree is what runs

Cron executes `scripts/paper.sh` from `/home/austin/trading-bot` directly. An
edit to `config.yaml` or `engine/` is in force at the next scheduled run
whether or not it is committed. There is no deploy step separating your working
tree from production.

Consequences:

- A half-finished edit left in the tree will trade.
- `git stash` changes live behaviour.
- Changes are effective before they are pushed, so "committed" is not the
  safety line — the edit is.

Run `python -m scripts.run_daily --dry-run` (and `--profile 2x`) after any
change to the gate, portfolio, or config. Dry runs are mutation-free: they roll
back journal writes and never touch reports.

## Read the journal, not the reports

`reports/paper*/YYYY-MM-DD.md` is a convenience log. `state/paper.db` is the
record. The daily job runs more than once a session, and reports only began
appending with the runs of 2026-08-04. Any file dated **2026-08-03 or earlier**
holds a single `# Paper run` block, and it is the *last* run of that day —
reliably the quiet one, every order having been placed hours earlier. Eight
sessions of real trading are journalled in those files as
`approved 0 | submitted 0`. They traded.

To ask what actually happened, query `orders`, `rejections` and `snapshots` in
the profile's SQLite journal.

## Cron: never delete one line of a pair

The server clock is UTC and Debian cron has no `CRON_TZ` (that is cronie).
Every ET slot therefore needs two crontab lines — one correct under EDT, one
under EST. Both fire year round; the ET slot passed as `$2` to `paper.sh` makes
the wrong-season copy exit as a no-op.

A pair looks redundant and is not. Deleting one silently breaks that job for
half the year. Give any new market-hours job a slot argument; leave jobs with
no market-clock sensitivity unguarded and single-line.

Back up the crontab to `state/crontab-*.txt` (gitignored) before editing.

## Research conventions

Studies live in `backtest/`, write one JSON to `reports/`, and carry a
`decision` field. The bar is pre-registered and deliberately hard:

> Higher Sharpe, no lower CAGR, no worse max drawdown, in **both** account
> profiles and **both** the `early_2020_2022` and `heldout_2023_plus` windows.

Costs are 15 bps per unit of one-way turnover, 3% annual short borrow, and
margin financing on 2x. Reuse `profile_returns` and `returns_summary` rather
than recomputing.

Existing decisions are binding. Several plausible ideas — rank buffers,
sector-neutral momentum, price-aware short selection, correlation caps,
defensive rotation, overnight equity, liquid pairs — have already been tested
and rejected or deferred, with reasons. **Check `reports/*.json` for an
existing `decision` before proposing a change.** Overriding one needs new
evidence, not a fresh opinion.

Campaigns are summarized in `reports/strategy_campaign_YYYY-MM-DD.md`.

### Validate a new study against the accepted one

If a study reimplements a sleeve, prove the control reproduces the existing
result before trusting its variants. `backtest/risk_overlay_study.py` matches
`build_portfolio` to 9e-8; without that check its differences could have been
implementation noise rather than the effect under test.

### A risk control is a strategy change

`backtest/production_portfolio.py` models MOM_LS as a pure weekly rebalance.
For months production also applied a stop-loss and a re-entry block, neither of
which the backtest simulated — so the reported Sharpe described a strategy that
was not running. Adding a control in `config.yaml` inherits no evidence from a
study that does not model it.

## The risk gate is the most important code here

`engine/risk.py` may reject or shrink a proposal. It may never enlarge one or
invent a symbol. `_assert_gate_invariants` enforces that contract at runtime
and will halt a run rather than send a malformed order.

When a change trips an invariant, **tighten it to express the new intent** —
do not relax it. Making MOM_LS stop-exempt did not remove "every entry has a
stop"; it became "an exempt sleeve carries exactly zero, everything else has a
valid stop."

`tests/test_risk_gate.py` is the gate on changes to that file. It feeds the
gate proposals a buggy or misbehaving model might realistically produce. Note
that `mom_ls` is stop- and block-exempt in the shipped config, so a test
asserting stop behaviour must name a still-stopped sleeve (`STOPPED_SLEEVE`).

## Sleeve-scoped config

Risk settings that suit one sleeve can be wrong for another. Prefer scoping to
switching a control off globally: `risk.stop_exempt_sleeves` and
`risk.reentry_block_exempt_sleeves` follow the existing precedent in
`engine/risk.py` where tsmom takes its own dollar-volume threshold.

Evidence usually covers one sleeve. Applying its conclusion everywhere invents
support the study does not provide.

## Account size is a real constraint

Both profiles hold about $10,000, so a MOM_LS slot is roughly $75. Gates sized
for institutional capital silently strangle the book:

- a $20M dollar-volume floor rejected ~20 names a day until 2026-08-03;
- Alpaca will not short fractionally, so a $75 slot cannot short anything
  priced above $75 — this biases the market-neutral sleeve net long, and is
  measured but unfixed (`reports/short_capacity_study.json`).

When a gate rejects a lot, check whether it is calibrated to this account
before assuming the signal is wrong.

## Verification

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m scripts.run_daily --dry-run
.venv/bin/python -m scripts.run_daily --profile 2x --dry-run
.venv/bin/python -m scripts.healthcheck
.venv/bin/python -m scripts.healthcheck --profile 2x
```

Studies over the full cross-sectional panel take minutes to tens of minutes;
run them in the background rather than assuming they hung.
