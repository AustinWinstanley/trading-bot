# Working on this repo

Operating notes for anyone — human or agent — picking this up cold. `README.md`
describes what the system *is*; this describes what will mislead you about it.

## This is live, and the working tree is what runs

Cron executes `scripts/paper.sh` from `/home/user/trading-bot` directly. An
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
than recomputing. Evaluate the promotion rule itself with
`backtest.promotion.passes_gate` / `passes_gate_all_cells` rather than
hand-rolling the comparison — seven studies each did that differently before
this module existed, so "passed the gate" didn't mean the same thing across
`reports/`. Not every candidate is a straightforward return enhancer; pick
the closest `objective_class` (`return_enhancer`, `risk_reducer`,
`cost_reducer`) and pre-declare its bounds before looking at results — see
the module docstring.

### `heldout_2023_plus` is no longer a clean hold-out

By the 2026-08-03 campaign, roughly fifteen studies had already used
`heldout_2023_plus` as part of a promotion decision. A window that has
arbitrated that many candidates is no longer meaningfully out-of-sample —
marginal accept/reject calls (hundredths of Sharpe) are close to
indistinguishable from selecting on noise, and nothing in the record
corrects for that multiplicity.

**2026-08-04 onward is reserved as a frozen final-validation window** — new
market data as it accrues, plus the live paper journal's actual results. No
study may tune its methodology, thresholds, or candidate variant on data
from that window. Treat `early_2020_2022` + `heldout_2023_plus` as a screen a
candidate must clear before anything else, not as sufficient evidence on its
own for promotion to `active`. A candidate that clears the screen moves to
shadow/paper observation and is judged against the frozen window before any
config change makes it live.

Existing decisions are binding. Several plausible ideas — rank buffers,
sector-neutral momentum, price-aware short selection, correlation caps,
defensive rotation, overnight equity, liquid pairs — have already been tested
and rejected or deferred, with reasons. **Check `reports/*.json` for an
existing `decision` before proposing a change.** Overriding one needs new
evidence, not a fresh opinion.

Campaigns are summarized in `reports/strategy_campaign_YYYY-MM-DD.md`.

### The cross-sectional panel has no COVID-crash coverage

`state/xsec/close.parquet` — the panel behind MOM_LS, the retired clone
sleeve, and every study that imports `backtest.xsec_data.load` — has no
usable data before **2020-07-27**. This isn't a handful of failed fetch
batches: every symbol checked, including AAPL and MSFT, has zero coverage
before that date. It lines up with Alpaca's free-tier IEX historical-bar
depth, matching the delisted-symbol cache's own mid-2020 floor
(`reports/survivorship_study.json`). `xsec_data.load()` drops the ~14 rows
before that date that did carry a stray symbol or two — keeping them
silently compressed multi-week returns into single "daily" observations,
which understated early-window drawdown and overstated Sharpe (verified:
panel-derived SPY max-DD moved from -24.5% to matching the true daily
-24.5% only once those rows were dropped; before the fix it silently used a
different, shallower number).

Dropping those rows is correct, but it means the `early_2020_2022` window is
shorter than its name implies for any panel-dependent sleeve, and contains
**no COVID crash observation at all** — the March 2020 trough is entirely
before the panel starts. Concretely, with the panel's actual span
(2020-07-27 → 2026-07-22): `early_2020_2022` has 614 bars, of which only
**341** have a live MOM_LS sleeve (its 252-day lookback + 21-day skip pushes
the first rebalance to 2021-08-25); `heldout_2023_plus` has 890 bars, all
live. Treat any MOM_LS or clone result attributed to `early_2020_2022` as
covering roughly Aug 2020–Dec 2022 with a thin first year, not the full
crash-inclusive window the label suggests. The only crash-era evidence for
these strategies is the French-Mom long-history proxy in
`long_history_stress_study.py`, itself only 0.55–0.60 correlated with the
sleeve it stands in for — read GFC/COVID rows from that study as directional,
not precise.

Pure price-history sleeves (SPY, TSMOM assets) are unaffected — they come
from Tiingo via `state/history*`, which goes back to 2010 or earlier, and
their return streams cover the crash normally.

### Cost schedule and two known, unresolved measurement gaps

The 15 bps figure above is the convention for cross-sectional stock
turnover. Other instruments intentionally use a different number and always
have: 8 bps for the TSMOM asset-ETF sleeve, 5 bps for the sleeve/portfolio
volatility overlays, 2 bps for single-name ETF pairs. These are deliberate,
not drift — but they were undocumented until now, so a reader diffing
`cost_bps` across studies had no way to tell "intentional" from "someone
typoed it." Record any new instrument-specific rate here when you add one.

Two known gaps are **not** resolved by this campaign and should be treated
as open questions, not settled conventions, in any study that touches them:

- **Risk-free rate is zero everywhere** (`returns_summary`, `engine.py`,
  `signal_library.py`, `portfolio_study.py`). Across a sample that straddles
  the 2022+ rate hike, this flatters `heldout_2023_plus` Sharpe (real cash
  and short-collateral earned ~5%, modeled as 0%) while also penalizing it
  (idle sleeves are modeled as literally 0-yielding). Fixing this needs an
  actual T-bill/EFFR series wired into `returns_summary` as an optional
  excess-return Sharpe — no such series is in `state/` yet
  (`state/french/` only has the momentum factor, not the 3-factor file with
  RF). Do not compare absolute Sharpe across windows without this caveat.
- **Short-borrow cost uses two different, disagreeing conventions.**
  `production_portfolio.py` charges a **constant** `0.15 * 3%` against the
  idealized fractional-short MOM_LS construction it evaluates
  (`backtest.xsec_momentum.build_portfolio`, which has no whole-share
  floor). `short_capacity_study.py` charges **realized, time-varying**
  `short_gross * 3%` against a separately-simulated whole-share-constrained
  $10,000-equity model, which per that same study only achieves ~60.9%
  (base) / ~83.5% (2x) of the target short gross. These are two different
  models of the same sleeve, not one model with a units bug — the headline
  README numbers come from the idealized fractional model, not the
  realistic capacity-constrained one. Reconciling them (either by porting
  the whole-share simulation into `production_portfolio.py`, or by
  explicitly labelling the headline numbers as an idealized-capacity upper
  bound) changes what every reported CAGR/Sharpe in this repo means, so
  don't fold that change into an unrelated study — it needs its own
  reviewed pass with the resulting headline numbers re-verified against
  `short_capacity_study.json`'s existing whole-share figures.

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
