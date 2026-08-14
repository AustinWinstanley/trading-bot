# Working on this repo

Operating notes for anyone — human or agent — picking this up cold. `README.md`
describes what the system *is*; this describes what will mislead you about it.

## This is live — but which deployment mode governs "what runs" depends on the server

This repo supports two deployment modes side by side (see
[docs/architecture.md](docs/architecture.md) and
[docs/operations.md](docs/operations.md) for the full mechanics of each).
**Check which one a given server is actually running before assuming either
invariant below** — `docker compose -f deploy/docker-compose.yml ps` showing
a running `engine` service means containerized; a `scripts/paper.sh` line in
`crontab -l` with no such container means legacy host-cron.

**Containerized (the current design, once a server has cut over):**
`config.yaml`/`config_2x.yaml`/`deploy/crontab` are baked into the `engine`
image, not mounted — so **the image is what runs**, not the working tree. An
edit to `engine/`, `scripts/`, `backtest/`, or either config file has zero
effect on production until a new image is built, pushed, and switched to via
`deploy/upgrade.sh`. `git stash`, an uncommitted edit, a `git pull` on the
server checkout — none of these touch the running system at all. The
server's git checkout still matters (it's what `journal` commits `reports/`
into, and what `deploy/upgrade.sh` reads `deploy/docker-compose.yml`/
`deploy/.env` from), but it is no longer the deploy artifact.

**Legacy host-cron (pre-cutover servers):** cron executes `scripts/paper.sh`
from the server checkout directly. The wrapper resolves its root from
`$PAPER_BOT_ROOT`, defaulting to a fixed production path when unset —
deliberately fixed, not self-deriving: two checkouts resolving different
roots would hold independent flock locks and could run `scripts.run_daily`
concurrently against the same live account (see the mutex comment at the
top of `scripts/paper.sh`). Cron never sets the override. Under this mode,
**the working tree is what runs**: an edit to `config.yaml` or `engine/` is
in force at the next scheduled run whether or not it is committed. There is
no deploy step separating your working tree from production.

Consequences under legacy host-cron specifically:

- A half-finished edit left in the tree will trade.
- `git stash` changes live behaviour.
- Changes are effective before they are pushed, so "committed" is not the
  safety line — the edit is.

**Either way**, run `python -m scripts.run_daily --dry-run` (and
`--profile 2x`) after any change to the gate, portfolio, or config, before
it can reach either deployment mode's production path. Dry runs are
mutation-free: they roll back journal writes and never touch reports.

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

## Crontab: one line per slot under supercronic, two under legacy host cron

This convention differs by deployment mode — see the "which deployment mode"
note above if you're not sure which one applies.

**Containerized:** `deploy/crontab` sets `CRON_TZ=America/New_York` once at
the top, and `supercronic` (reading it inside the `engine` container)
resolves DST via Go's IANA tzdata — **one** line per ET slot is correct
year-round. `tests/test_deploy_crontab.py` statically checks this file
(`CRON_TZ` present, no duplicate job/slot pairs, every slotted job's cron
fields matching its own slot argument) — run it after editing
`deploy/crontab`, and rebuild+redeploy the `engine` image for the edit to
take effect at all (see the "image is what runs" note above).

**Legacy host cron:** the server clock is UTC and Debian cron has no
`CRON_TZ` (that is cronie). Every ET slot therefore needs **two** crontab
lines — one correct under EDT, one under EST. Both fire year round; the ET
slot passed as `$2` to `paper.sh` makes the wrong-season copy exit as a
no-op. A pair looks redundant and is not — deleting one silently breaks
that job for half the year. Give any new market-hours job a slot argument;
leave jobs with no market-clock sensitivity unguarded and single-line. Back
up the crontab to `state/crontab-*.txt` (gitignored) before editing.

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
`cost_reducer`, `diversifier`) and pre-declare its bounds before looking at
results — see the module docstring. The `diversifier` class exists because
max drawdown is the noisiest single-path statistic the gate judges and
zero tolerance on it systematically rejects new lowly-correlated streams;
its drawdown tolerance must come from
`backtest.promotion.paired_drawdown_noise_pp` (a paired block-bootstrap
noise band), never a hand-picked number, and its Sharpe/CAGR checks stay
strict.

### Why externally-sourced strategies keep failing here — four filters

The 2026-08-12/13 new-strategy campaign studied four externally-motivated
directions and rejected or deferred all of them, prompting the fair
question "are we testing wrong?" Mostly no — the failures decompose into
four recurring causes, each now demonstrated by this repo's own reports.
Before proposing an external strategy, state which of these it must
survive and which objective class it will be judged under:

1. **Selection bias and post-publication decay.** Strategies you hear
   about are the survivors, and published anomalies decay. Own evidence:
   pre-FOMC drift ran t=3.21 pre-2015 and t=0.21 after
   (`pre_fomc_drift_study.json`); turn-of-month never cleared t=1.15 in
   any decade (`turn_of_month_study.json`). Filter: demand the effect
   exist in the RECENT subperiod, not just the long average.
2. **Beta mistaken for alpha.** "People make money trading X" usually
   means X went up. Own evidence: the crypto trend sleeve MADE money
   (+12.7% CAGR, 0.49 Sharpe standalone) and was still correctly rejected
   as a portfolio addition (`crypto_trend_study.json`,
   `crypto_diversifier_study.json`) — the question this repo asks is
   never "does it make money" but "does adding it improve THIS
   portfolio." Filter: judge the marginal portfolio effect, not the
   standalone stream.
3. **Constraint mismatch.** Edges that are real for someone else can be
   unharvestable here: $10k account, whole-share shorts, universe
   filters, ~09:51/10:05 ET fills, honest costs. Own evidence: insider
   echo reproduced the published +74bps effect and showed the tradeable
   subset (price >=$5, ADV >=$3M) captures -2bps of it against 30bps of
   costs (`insider_echo_study.json`); overnight drift's break-even is
   ~2bps/leg (`overnight_cost_study`). Filter: simulate under the LIVE
   gate's universe/fill/cost constraints before believing any number.
4. **Pareto-gate strictness on noisy statistics.** The one cause that was
   ours: `return_enhancer` demands strict improvement on 12 simultaneous
   conditions including zero-tolerance max drawdown — BIL failed by 1bp,
   target restoration failed on 0.2-0.6pp despite 4/4 Sharpe+CAGR wins.
   The fix is the `diversifier` class above, NOT loosening
   `return_enhancer` (which remains the bar for modifying existing
   sleeves). Post-fix evidence that the diagnosis was right and bounded:
   re-judged as a diversifier, crypto's drawdown objection fully
   dissolved into noise (4/4 cells within band) and the remaining
   rejection is a real early-window return drag — a genuine finding, not
   an artifact (`crypto_diversifier_study.json`).

A hypothesis that was FOUND by mining this repo's own history gets one
extra requirement: a placebo/permutation battery and a forward-only
confirmation path, never in-sample re-confirmation — see
`fomc_trend_off_study.json` (survived a 10,000-draw permutation placebo
at p=0.011-0.012, worth ~$44/yr at realized event rates, and still
authorizes nothing but an observation-only forward log on post-2026-08-13
data it has never seen).

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
sector-neutral momentum, price-aware and concentrated whole-share short
selection, correlation caps, defensive rotation, overnight equity, liquid
pairs, and the fixed intraday ETF families — have already been tested and
rejected or deferred, with reasons. The 1DTE translator stands down because no
directional intraday family qualified. News passed a data-feasibility check but
not a return study, and the 0DTE surface collector is read-only observation.
**Check `reports/*.json` for an existing `decision` before proposing a
change.** Overriding one needs new evidence, not a fresh opinion.

### The 2026-08-04 frozen window was substantially spent by 2026-08-12

An audit of the 2026-08-12 campaign found the freeze above was honored in
letter more often than in spirit. Two concrete violations, both since
addressed:

- The execution-timing experiment generated its hypothesis from the frozen
  live journal, measured its effect on that same frozen data, revised its
  matching methodology after seeing an interim result computed on it, and
  set its promotion threshold below an already-observed effect size — four
  of the policy's five prohibitions in one candidate.
- A live `engine/portfolio.py` liquidity-floor change was made in direct
  response to FXE's behavior observed only in the frozen paper journal,
  before any study of the *shipped* variant existed (the backing study
  tested a different, pre-normalization construction and had zero
  discriminating power on the cached data — see
  `reports/tsmom_liquidity_alignment_study.json`'s `no_effect`/
  `all_no_effect` fields).

More broadly: the entire 2026-08-12 equity-side candidate slate (breadth,
capacity-matching, target restoration) was *sourced* from reading the frozen
window's rejection/loss patterns. Each individual study then correctly
avoided tuning on 2026-08-04+ data — but candidate selection is itself a
researcher degree of freedom, and nothing in the record flags that the
candidates themselves were chosen by looking at the window meant to
validate them. This doesn't make those studies' conclusions wrong; it means
their eventual validation against 2026-08-04..08-12 specifically would be
partly circular.

**2026-08-13 onward is the new frozen final-validation window.**
2026-08-04 through 2026-08-12 is demoted to screening status — usable the
way `early_2020_2022`/`heldout_2023_plus` are used (a bar a candidate must
clear, not evidence on its own), but no longer treated as untouched. The
same prohibitions apply to the new window: no study may tune its
methodology, thresholds, or candidate variant on 2026-08-13+ data, and
candidate *selection* should be justified independently of what that window
shows before it accrues meaningfully, not reverse-engineered from it.

Campaigns are summarized in `reports/strategy_campaign_YYYY-MM-DD.md`.

### A fixed-N gate is the wrong tool for a low-frequency signal

`reports/intraday_strategy_study.json` rejected IWM compression breakout on
trade count alone (18 and 24 trades against a `>=30` fixed gate) despite
positive profit factor in both windows — at its ~1.5 trades/month firing
rate, a fixed N=30 threshold needs ~20 months to ever resolve either way.
`reports/iwm_compression_breakout_forward_test_registration.json` +
`backtest/iwm_compression_breakout_forward_test.py` register a Sequential
Probability Ratio Test (Wald SPRT) instead, on IWM compression breakout's
post-2026-08-13 forward trades specifically — it can stop earlier if the
effect is strong, and never needs an arbitrary trade-count floor if it's
weak, while keeping pre-declared, honest false-positive/false-negative
rates. `alpha`/`beta`/`mu0`/`mu1`/`sigma` are fixed by that
registration document; changing any of them requires a new, separately
reviewed registration, not an edit to the existing one.

The signal generator the registration called for is
`scripts/iwm_breakout_forward.py` (2026-08-14): a read-only recorder that
imports the accepted `compression_breakout_signal` +
`simulate_fixed_horizon` (no reimplementation to validate), journals the
frozen spec's would-be trades at the registered 5bp/leg stress cost into
`state/iwm_breakout_forward.db`, and prints `monitor()`'s decision each
run — loudly CRITICAL on a boundary crossing, with no automatic action
(promotion to shadow is a human-reviewed step). Because the spec is
deterministic on completed five-minute bars, it only processes sessions
strictly before today (ET): every session it touches is final, so the
`iwmfwd` job in `scripts/paper.sh` is market-clock-insensitive — one
unslotted crontab line (e.g. `30 23 * * *`), no EDT/EST pair. It records
sessions from 2026-08-13 onward only and self-backfills any gap since its
last run, so a missed day loses nothing.

### News headline classifier precision, hand-checked against real data

`reports/news_pead_feasibility.json`'s own limitations flagged "headline
regex precision must be manually sampled before a return study" — done
2026-08-12, `reports/news_pead_feasibility_precision_check.json`. A 120
-headline random sample of `is_earnings_result_headline`'s positives from
the real cached `state/news/alpaca_news_2026-01-01_2026-07-31.parquet`
dataset (147,305 articles) was hand-checked one by one; ground truth is in
`backtest/news_pead_feasibility_precision_labels.py` (kept out of the
gitignored `state/news/` cache directory since it's a real, reproducible
research artifact, not a data cache). Baseline precision was
81.7% (22/120 false positives), concentrated in a recognizable Benzinga
templated preview-article family ("Earnings Outlook For X", "Insights
Into X's Earnings", "A Glimpse/Peek/Look Ahead ... Earnings", "... Ahead
Of Earnings") plus "Price Over Earnings Overview" (P/E-ratio commentary,
not an earnings report at all). Six new `FORWARD_PATTERNS` entries fixed
all of them on the same sample (100% precision), at the disclosed cost of
2 lost true positives whose headlines happen to contain "outlook" in a
genuine post-release context — an accepted precision-over-recall
trade-off, matching the module's own "high-precision label" design goal.
Re-running the full audit against the same 147,305-article cache dropped
the single-symbol event count from 11,102 to 8,606 (still far above the
500-event gate) — the feasibility decision itself
(`proceed_news_conditioned_event_study`) is unchanged, now backed by
measurably higher-precision data. Not yet done: an independent, freshly
-drawn out-of-sample precision check (the fix was verified in-sample) —
flagged in the precision-check report, not treated as settled.

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
volatility overlays, and 2 bps for single-name ETF pairs. Intraday ETF screens
use 2 bps **per leg** as the primary assumption and must also survive a
mandatory 5 bps **per leg** stress. These are deliberate, not drift — but they
were undocumented until now, so a reader diffing `cost_bps` across studies had
no way to tell "intentional" from "someone typoed it." Record any new
instrument-specific rate here when you add one.

### Intraday and options research is observation, not activation

`backtest/intraday.py` uses a shared five-minute SPY/QQQ/IWM IEX panel covering
2024-02 through 2026-07. It has no 2020 or 2022 stress regimes and is not
consolidated SIP data. Signals use completed bars and enter at the next bar's
open, but five-minute OHLC data cannot resolve stop-versus-target ordering
inside a bar. Treat results as screening evidence, not fill validation.

Option shadow jobs cannot submit paper orders. They collect displayed quotes
or recommendations, and quote qualification means only that a surface met its
declared spread, size, credit, or loss bounds — not that the trade has positive
expected return. A qualified research signal may authorize observation; it
does not authorize automatic paper activation. The committed launch/decision
JSON and current config are the authority for each component's status.

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

### The experiment tier is a second, lighter-weight evidence bar — not a bypass

`config_2x.yaml`'s top-level `experiments:` block (`engine/config.py`'s
`ExperimentConfig`, parsed by `_parse_experiments`) is a formal second tier,
distinct from the hard promotion gate above. It exists because the 2x
account is deliberately the aggressive lab (base stays the unchanged
control) and because paper-trading learnings are valuable even when a
signal hasn't cleared a full frozen-window study — but that lower bar has to
be capped and pre-committed, not open-ended:

- **Three statuses**: `off` (no proposals ever generated for this sleeve —
  the default and the only status that needs no registration file),
  `shadow` (read-only observation; the gate rejects any real buy/short whose
  sleeve resolves to a shadow experiment, so a bug that tries to size one
  cannot reach the broker even by accident), `paper` (real paper orders,
  sized and capped by the gate).
- **A registration document is required before `status` can leave `off`.**
  `_parse_experiments` raises `ConfigError` at load time if the file at
  `registration:` doesn't exist — the same enforced-honesty pattern as the
  frozen-window promotion gate, just checked earlier and more cheaply.
  Follow the existing `reports/*_shadow_launch.json` shape: hypothesis,
  measurement plan, review bar, known gaps.
- **`allocation_pct` is a hard per-experiment gross-exposure cap**, enforced
  by `engine/risk.py`'s gate as a shrink step (same "tighten, never relax"
  philosophy as every other cap in that file) — never just documentation.
  Total `allocation_pct` across every non-`off` experiment is capped at
  `MAX_TOTAL_EXPERIMENT_ALLOCATION_PCT` (30% as of 2026-08-12); core
  allocation always keeps the majority of lab equity.
- **`max_cumulative_loss_pct` drives an automatic stand-down.**
  `scripts/run_daily.py` computes realized-plus-unrealized P&L per
  experiment each run (`experiment_realized_pnl` persisted in
  `risk_state_2x.json`, unrealized from currently-held positions via their
  most recent journal-recorded sleeve — positions carry no sleeve tag from
  the broker, so the journal is the only record) and calls
  `engine.risk.compute_experiment_standdowns`. A breach adds the experiment
  to `RiskState.experiment_standdowns`, which blocks new entries in the
  same run and — critically — the daily runner immediately injects
  sell/cover proposals for every position attributed to that experiment
  (section 4a-bis, same priority as a software stop), so a breach flattens
  the sleeve instead of waiting for the next signal to happen to reduce it.
  Un-standing-down requires a human: raise `max_cumulative_loss_pct` (a
  reviewed config change, not automatic) or set `status: off`.
- **Promotion to core allocation still requires the hard gate** on the
  (re-frozen, see above) validation window. Nothing about clearing an
  experiment's own review bar substitutes for that — the experiment tier
  answers "is this worth learning from live," not "is this worth trusting."
- Sleeve attribution uses the same `+`-joined exact-membership matching as
  the stop/re-entry exemption sets (`engine/risk._experiment_for_sleeve`,
  `_sleeve_contains`) — a combined sleeve like `"mom_ls+bull_put_live"` is
  still correctly governed by its experiment part.

As of 2026-08-12, `bull_put_delta_selected_live` (below) is the first
`paper`-status entry — registered ahead of its own shadow's declared
evidence bar, with that gap explicitly disclosed in its registration doc
rather than silently promoted.

### Real options paper trading: engine/options_risk.py and scripts/options_daily.py

Every options script before 2026-08-12 (`scripts/options_shadow.py` and
friends) was read-only quote collection — `tests/test_shadow_read_only.py`
statically enforces that. `scripts/options_daily.py` is the first
genuinely new order-submission code path in this repo beyond equities: it
submits real (paper-broker) multi-leg options orders via Alpaca's
`order_class: "mleg"` endpoint, for the 2x lab's `bull_put_delta_selected_live`
experiment only. Base account (`config.yaml`) has zero options capability —
hard-coded in `scripts/options_daily.py`'s `--profile` choices, not a
config default that could silently drift.

- **A parallel gate, not an extension of the equity one.** `engine/options_risk.py`'s
  `evaluate_option_structure` is a sibling to `engine/risk.py`'s `evaluate()` —
  `engine/risk.py` itself has zero lines changed for any of this. It reuses
  (does not reimplement) `ExperimentConfig`, `_experiment_for_sleeve`, and
  `compute_experiment_standdowns` from the equity gate. A structure whose
  sleeve resolves to no registered experiment is rejected outright — there
  is no "core options allocation" the way there is for equities.
- **Defined-risk-only is the load-bearing invariant**: `maximum_loss` must
  be finite and positive or the structure is refused, asserted both inline
  and again at the end via `_assert_option_gate_invariants` (same
  belt-and-suspenders pattern as `engine/risk.py`'s own invariant check).
- **At most one open structure per experiment at a time**
  (`MAX_CONCURRENT_STRUCTURES_PER_EXPERIMENT`), enforced in the gate. No
  laddering, no rolling, no profit-target early close in this first version —
  bounds worst-case loss to exactly one `allocation_pct` by construction and
  keeps the reconciliation logic below simple (0-or-1 structure, never N).
- **Exit is close-by-DTE, not backtested.** `options_experiments.bull_put_delta_selected.close_by_dte_trading_days`
  (5, as of 2026-08-12) forces a close well before expiration — a
  risk-management convention, not a claimed-optimal number; see
  `reports/bull_put_fixed_width_study.json`'s own limitations on American
  early assignment not being reconstructed in this repo's backtests.
- **Assignment handling is detection, not automatic remediation, in this
  first version — deliberately.** `scripts/options_daily.py`'s
  `reconcile_option_structures` (also imported into `scripts/healthcheck.py --profile 2x`
  for a second daily check) verifies broker positions match the journal's
  open structures and flags any unexplained equity position in an
  underlying with an open structure. Any anomaly is a loud `CRITICAL` with
  no automatic trading response — this repo has never observed whether
  Alpaca's paper broker simulates early assignment at all or only settles
  at expiration, and encoding an unverified assumption into autonomous
  "smart" remediation would be a worse risk than a page a human reviews.
  "Unexplained" is now literal, not just documentation intent: SPY is
  both this experiment's only underlying and `equity_core`+`trend`'s core
  holding, so the first day the spread went live (2026-08-14) this check
  correctly-by-the-letter but uselessly-in-practice flagged the account's
  ordinary ~15-share SPY position as a possible assignment, tripping
  `health2x` CRITICAL for a non-event. Fixed by computing
  `equity_explained_qty` — net quantity the equity journal's own filled
  orders (`buy`/`cover` add, `sell`/`short` subtract) already account for
  in that symbol — and flagging only the delta beyond it
  (`scripts/options_daily.py`'s `equity_qty_explained_by_orders` /
  `load_equity_explained_qty`), with a 0.5-share tolerance that's still
  two orders of magnitude below a real assignment's 100-share move. A
  missing or schema-less equity journal (a profile that's never traded)
  falls back to the original, stricter "everything is unexplained"
  behavior rather than silently suppressing a real finding.

  The same day surfaced a second, related false positive:
  `scripts/healthcheck.py`'s `assess_health` also flagged both open
  option legs themselves as "no broker or fallback stop" — true by the
  letter (no stop order exists), but a non-event by design. Option legs
  are defined-risk by the spread structure's own `maximum_loss`, never by
  an equity-style stop; `scripts/options_daily.py` has no stop-submission
  path for legs at all. `assess_health` now skips this check for any
  position whose `asset_class` is `us_option` — an orphaned or
  mismatched leg is a real problem, but it's `reconcile_option_structures`'s
  job to catch that (its own missing-leg / wrong-sign-leg findings), not
  this equity-shaped check's.
- **Options-level pre-flight is real, not assumed.** Alpaca requires
  options trading Level 3 for multi-leg spreads; nothing checked this
  before `scripts/check_options_level.py` (run by hand once) and the same
  check running inline in `scripts/options_daily.py` on every single run
  (defense in depth, not a one-time gate). Confirmed on the real 2x paper
  account on 2026-08-12: `options_approved_level=3`.
- **`scripts/paper.sh`'s `options_daily2x` job deliberately shares
  `daily2x`'s flock lock, not its own** — unlike the read-only `shadows2x`
  job. It writes real orders and read-modify-writes the same
  `state/risk_state_2x.json` keys (`experiment_realized_pnl`,
  `experiment_standdowns`) `scripts/run_daily.py --profile 2x` owns; an
  independent lock would let the two race that file and submit to the same
  account concurrently. If a future change gives this job its own lock,
  re-read this reasoning first — it was a deliberate, not a default, choice.
- **`bull_put_delta_selected_live` was promoted at 3 of its own declared 20
  shadow observations (1 of 3 required expirations)** — a disclosed
  exception, not a claim the bar was met; see
  `reports/experiments/bull_put_delta_selected_live.json`'s
  `evidence_at_promotion_2026_08_12` field. Distinct from
  `bull_put_fixed_width`, whose backtest (`reports/bull_put_fixed_width_study.json`)
  already returned `insufficient_evidence` with 0 completed spreads — that
  structure is not a live candidate at all, not merely under-observed.
- The put broken-wing butterfly, event-vol, and 0DTE shadows are untouched
  by any of this and stay shadows until their own bars are met.
  `momentum_verticals` stays `off`.

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

### `risk.restoration_exempt_sleeves` — the one exemption that loosens a gate

Every other sleeve-scoped control in this section narrows or redirects a
check. `risk.restoration_exempt_sleeves` (`RiskLimits.averaging_down_permitted_for`,
`engine/risk.py`'s two "averaging down" checks) is the one that removes a
check — the otherwise-unconditional "reject because this position is a
loser" block — for listed sleeves. `config_2x.yaml` sets `[mom_ls]`;
`config.yaml` deliberately does not set this at all, since base is the
unchanged control. It adds no new sizing logic: a rebalance proposal is
always `target - held`, and mom_ls's target has no reference to a
position's P&L, so removing this one check does not let a proposal request
more than the position would already be entitled to as a non-loser —
every other cap (position/exposure/leverage) still applies on top. See
reports/target_restoration_study.json and the "experiment tier" section
above for the evidence and why this was judged worth the lab's lighter bar
despite failing the hard promotion gate's zero-tolerance drawdown rule.

### `scripts/weekly.py`'s mom_ls rebuild is now profile-aware

Before 2026-08-12, `build_mom_ls_targets()` always loaded `config.yaml`
regardless of which profile invoked it, and both profiles' `paper_portfolio.
mom_ls_targets_file` pointed at the same `state/mom_ls_targets.json` — so a
2x-only change to `mom_ls_top_n` would have been silently inert (the shared
file was always built from base's parameters) or, worse, would have
clobbered the shared file with whichever profile's weekly run happened to
execute last. `build_mom_ls_targets(cfg)` now takes an explicit config, and
`main()` only runs the 2x-specific rebuild when `config_2x.yaml`'s
`mom_ls_targets_file` differs from `config.yaml`'s — see `_mom_ls_params`
and the CRITICAL guard in `main()` for the case where parameters diverge
but the file path was left shared (a misconfiguration, not something to
paper over). Any future 2x-only mom_ls construction change (breadth,
filters, cadence) needs its own `mom_ls_targets_file`, not just a changed
parameter.

### The 2x lab rebuilds MOM_LS twice a week; base stays weekly

`scripts/paper.sh`'s `momls2x` job runs `scripts.weekly --mom-ls-only`
mid-week (Wednesday, unslotted like `weekly` — this is a data job, not
market-hours sensitive), rebuilding only `config_2x.yaml`'s
`mom_ls_targets_file`. `weekly`'s existing Sunday run still does the same
rebuild as part of its full flow, so between the two, the 2x lab's MOM_LS
ranks refresh roughly twice a week instead of once; `config.yaml`/base
never runs `--mom-ls-only` and stays on its original weekly-only cadence,
unchanged. `momls2x` deliberately shares `weekly`'s flock lock (both write
the same file and must never race).

Backed by `reports/mom_ls_cadence_study.json`: genuinely mixed screening
evidence (helps `early_2020_2022`, both profiles; marginally hurts
`heldout_2023_plus`, both profiles — Sharpe swings of a few hundredths
either way, not a clear signal). The rationale for trying it anyway is the
live-journal finding that the weekly rebuild is the dominant source of
trade-frequency variance (Mondays: 11-23 orders; other days: 0-5) and the
only source of new names all week — the extra turnover cost (+0.33pp/yr
base, +0.63pp/yr 2x CAGR drag per the study) is modest and roughly the same
order of magnitude as the backtested Sharpe/CAGR swing, so cost alone
doesn't settle it. This is explicitly a "learn from live fill quality and
turnover, not validated by backtest" trial under the experiment-tier bar —
screening-tier evidence (pre-2026-08-13), not a hard-gate promotion.

### The 2x lab invests idle trend-sleeve cash in BIL; base stays idle

`engine/portfolio.py`'s `trend_targets` gained an optional
`paper_portfolio.trend_reserve_symbol` config key. When the trend sleeve's
signal is off (its symbol below its moving average) and a reserve symbol
is configured with available bars, the sleeve's weight goes into the
reserve symbol instead of sitting idle at 0% — falling back to idle cash if
the reserve's own bars are unavailable that day, matching every other
sleeve's no-data-means-no-position discipline. `config_2x.yaml` sets `BIL`;
`config.yaml` does not set this key at all — idle cash stays idle there.

Backed by `reports/bil_idle_cash_decision.json`: `reports/cash_reserve_study.json`'s
BIL candidate beat the control on Sharpe AND CAGR in both screening
windows, and was rejected by the pre-registered return_enhancer hard gate
for a single 1bp-worse drawdown in one window (-0.1254 vs -0.1253) — not
economically distinguishable from noise for a near-zero-duration T-bill
substitute. Same razor's-edge-near-miss-with-strong-other-axis-evidence
pattern as the MOM_LS breadth and TSMOM-FXE-drop decisions above; judged
under the lab's lighter experiment-tier bar, not (as Phase 4's plan text
first suggested) `backtest/promotion.py`'s `risk_reducer` objective class —
that class is shaped for a candidate that trades CAGR away for less
drawdown, and BIL's result is the opposite shape (CAGR improves, not
costs), so forcing it into that framework would mean inventing a cost
budget for a candidate that isn't paying one. This is a pure
portfolio-construction parameter, like MOM_LS breadth and the TSMOM
universe change — not a capped side-bet, so it is not wrapped in the
`experiments:` framework.

### `equity_core` + `trend` silently capped at a quarter of their target

Found in a 2026-08-03 review of the server journal: `max_position_pct` (15%
base / 30% 2x) is a single-name concentration control, but `equity_core`
(0.40) and `trend` (0.20) both hold SPY, so their combined target — 60% base,
120% 2x — sat well above it. The gate did exactly what it's supposed to:
shrunk every SPY buy back down to the cap. Live effect, confirmed from
`state/paper.db`/`state/paper_2x.db`: SPY was rejected at the position cap on
19 of the last ~19 base-profile opportunities and 14 of ~14 on 2x
(2026-07-24 through 2026-08-03), realizing ~15%/~31% instead of the 60%/120%
target — every un-invested difference sat in cash instead.

Fixed with `risk.elevated_position_pct` / `risk.elevated_position_sleeves`
(same shape as the stop/re-entry exemptions above): a symbol whose sleeve
attribution is *entirely* within the listed sleeves gets the wider cap —
`RiskLimits.position_cap_pct` requires every `+`-joined origin to qualify, so
a name that's part index-core and part something else does not inherit it.
Config validation refuses one of the pair without the other, and refuses an
"elevated" value below the base cap — see `engine/config.py`.

This was a config/backtest divergence of the same shape as the MOM_LS
stop-loss finding: `backtest/production_portfolio.py` has never modeled a
position cap at all, so every headline number already assumed SPY reached
its full target. The gate, not the backtest, was silently describing a
different portfolio. Re-run `scripts.run_daily --dry-run` after touching
either `elevated_position_pct` value and check the SPY proposal is no longer
shrunk for a cap reason before trusting a change here.

### The IPO-age gate now uses a real, if approximate, listing date

`scripts/run_daily.py` used to hardcode every symbol's `listed_days` to
10,000, so `universe.exclude_ipo_days` (180) could never fire in production
even though `tests/test_risk_gate.py` exercises it directly. Alpaca's asset
endpoint carries no listing date at all, so there's no single free field to
read. The fix fetches bars over a window comfortably longer than
`exclude_ipo_days` (currently `max(400, exclude_ipo_days * 2 + 40)` calendar
days) and uses the first bar's date as `listed_days` — the same proxy
`scripts/weekly.py` already relies on for universe history length. Verified
against a real recent listing: CRCL's first bar in that window lands on its
actual 2025-06-05 IPO date. A symbol with no bars at all is treated as 0
days (fails the gate) rather than as old — the liquidity gate would reject
it anyway, and assuming "old" on missing data is the wrong direction to fail
in. Note the proxy only resolves the threshold, not a precise listing date:
a genuinely old symbol's `listed_days` is a lower bound (however far back
the window reaches), which is fine since it still clears 180 either way.

## Account size is a real constraint

Both profiles hold about $10,000, so a MOM_LS slot is roughly $75. Gates sized
for institutional capital silently strangle the book:

- a $20M dollar-volume floor rejected ~20 names a day until 2026-08-03;
- Alpaca will not short fractionally, so a $75 slot cannot short anything
  priced above $75 — this biases the market-neutral sleeve net long, and is
  measured but unfixed (`reports/short_capacity_study.json`).

When a gate rejects a lot, check whether it is calibrated to this account
before assuming the signal is wrong.

## Read-only monitoring & debug surfaces

Two Docker Compose services run alongside the cron jobs, both entirely
read-only by construction — neither can place an order, touch the Alpaca
client, or write to a journal, no matter what a bug in either does:

- **`dashboard`** (`dashboard/`, port 8787) — a Flask JSON API + single-page
  UI (`dashboard/templates/index.html`) showing both profiles' equity,
  positions, trade feed, exposure, risk budget, and health status. This has
  existed since early in the project but was never documented here until
  now.
- **`mcp-server`** (`mcp_server/`, port 8788) — an MCP (Model Context
  Protocol) server exposing the same data as live tools an AI assistant can
  call directly during the trading day, plus capabilities the dashboard
  doesn't have: ad hoc read-only SQL against the journals
  (`query_database`), raw `state/`/`config.yaml`/`reports/` file reads,
  and `logs/paper-*.log` tailing. Built specifically so debugging a live
  issue (like the 2026-08-13 incident this section indirectly documents —
  see `dashboard/db.py`'s module docstring) doesn't require manually
  copying SQLite files off the server.

Both services share one security posture, stated once in
`deploy/docker-compose.yml`'s `dashboard` block and not repeated per
service: **LAN-only, zero login/auth by design.** Anyone who can reach the
server's IP on this network sees live paper-account data with no gate at
all — deliberate, chosen over Tailscale/SSH-tunnel for same-network access
without a login. If either service ever needs to be internet-facing, add
auth first; don't just widen the port mapping.

`mcp_server/` reuses `dashboard/db.py`'s pure functions (including the
`*_payload` functions both `dashboard/routes.py` and `mcp_server/tools.py`
call — one definition of what each response means, not two that can
drift) but never imports `dashboard.routes` or `dashboard.app`, so it
carries no Flask dependency. Both packages are covered by the same class
of AST-based safety test (`tests/dashboard/test_safety.py`,
`tests/mcp_server/test_mcp_safety.py`) that statically bans importing
`engine.execute`, `engine.data`, `scripts.run_daily`, or
`scripts.healthcheck` — anything that can reach the Alpaca client.

`mcp_server`'s new primitives layer read-only guarantees rather than
relying on just one: `query_database` only accepts `SELECT`/`WITH`
statements (`mcp_server/debug.py:run_select`), on top of the same
`mode=ro` SQLite connections (`dashboard/db.py:open_ro`) that already
refuse any write at the VFS level regardless of that check, and on top of
`sqlite3.Cursor.execute()` itself refusing to run more than one statement
(so `"SELECT 1; DROP TABLE t"` fails before either guard even matters).
File reads (`read_state_file`, `read_report`, `tail_trading_log`) are all
path-traversal-guarded to stay under `state/`/`reports/`/`logs/`
(`mcp_server/debug.py:_resolve_within`).

Bring both up (from the server checkout):

```bash
docker compose -f deploy/docker-compose.yml up -d --build
```

Register the MCP server with Claude Code (user-level, not a
repo-committed `.mcp.json` — this is a LAN address specific to one
server, not something to bake into git history):

```bash
claude mcp add --transport http trading-bot-debug http://<server-ip>:8788/mcp
```

## Commit messages: Conventional Commits drive the release version

Every commit subject follows [Conventional Commits](https://www.conventionalcommits.org/):
`type(scope): summary`, imperative mood, lowercase type. `scope` is optional
and usually a top-level area (`engine`, `risk`, `mom_ls`, `dashboard`,
`deploy`, ...). Release automation (`release-please`) reads these directly
off `main` to decide the next semantic version and to write the changelog —
get the type right, not just the prose.

- **`fix:`** — a bug fix. Triggers a patch release.
- **`feat:`** — a new capability (a new sleeve, a new experiment, a new
  dashboard view). Triggers a minor release.
- **`feat!:`** or a `BREAKING CHANGE:` footer — an incompatible change
  (a config schema change, a removed script flag). Triggers a major
  release. Rare in a project without a public API, but real for things
  like `config.yaml` schema or the CLI surface of `scripts/`.
- **`chore:`** — no production code change (`.gitignore`, dependency
  bumps, tracked-report housekeeping). Does not trigger a release.
- **`docs:`**, **`test:`**, **`refactor:`**, **`perf:`**, **`style:`** —
  standard Conventional Commits types, used for exactly what they say.
  None trigger a release on their own.
- **`chore(journal): paper reports`** — the automated daily/weekly journal
  commits described elsewhere in this file. Never hand-write one of these;
  they're machine-generated and excluded from the changelog by convention.

A **research decision** (a study's `reports/*.json` plus whatever config
change it authorizes) is usually `feat:` if it activates or changes a
sleeve/experiment, or `chore:`/`docs:` if it's a pure rejection with no
production code change — a rejected candidate that only adds a report file
is `docs:` or `chore:`, not `feat:`.

This convention is enforced (a lint check on PRs, advisory on direct pushes
to `main`) but the release automation only cares about `main`'s history —
getting the type right on a direct push matters exactly as much as on a PR.

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
