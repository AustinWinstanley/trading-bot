# Strategy candidates and continuity notes

Written 2026-09-22, one week into the M6 paper validation. This is the
hand-off for whoever picks up strategy research next: what is running and
why it is not yet an edge, what has already been judged (do not re-run
it), what is worth studying and in what order, and the operational items
that were open on this date. Update it when a candidate is registered,
decided, or dropped; the JSON `decision` fields under `reports/` remain
the authority, this file is the map to them.

## Where things stand

- **Live book (both profiles, since 2026-09-15):** M6 — 0.80 SPY +
  0.20 `lev_trend` (QLD while QQQ > 200-day SMA, else BIL). Selected by
  `reports/portfolio_mix_study.json` under the `benchmark_beater`
  objective; the only mix that cleared every cell. Full chain in
  [research.md](research.md#the-absolute-return-campaign-2026-09-14-and-the-m6-portfolio).
- **Read it honestly.** M6 is ~1.2× index beta with a slow crash filter.
  Its first live week (+3.2% vs SPY +1.7%, base) is the market going up,
  not evidence. Its edge in the backtest (+2.4 pp/yr, 2023+ Sharpe margin
  of 0.02) comes entirely from the trend switch, which has not fired live
  yet. Weeks with zero orders are normal: two holdings, a 20%-of-target
  drift band capped at 5% of equity, and a switch that fires a few times a
  year.
- **Verdict mechanism:** `reports/absolute_return_paper_validation_registration.json`
  — kill rules K1–K4, 63-session primary window, 126-session extension,
  sessions counted as trading days (fixed 2026-09-21, v1.4.1). Weekly
  status in `reports/paper/weekly-*.md`. Nothing in this file overrides
  that registration.
- **Frozen window:** 2026-08-13 onward. No study may select, tune, or
  confirm a candidate on it. `early_2020_2022` + `heldout_2023_plus` are
  screens, and the 2023+ window has arbitrated ~15 decisions already — a
  margin of a few hundredths of Sharpe there is noise (AGENTS.md).

## Already judged — do not re-propose without new evidence

46 studies carry a `decision` (list them with a one-liner over
`reports/*.json`). The families below are closed:

| Family | Decision | Report(s) |
| --- | --- | --- |
| Cross-sectional stock momentum (long/short, long-only, vol-scaled, buffers, breadth, concentration, cadence, capacity, sector-neutral, MTUM) | rejected / stood down 2026-09-14 | `long_only_momentum_study`, `momentum_*`, `mom_ls_cadence_study`, `capacity_matched_momentum_study` |
| Asset-class trend (TSMOM), Keller HAA | rejected vs SPY | `haa_study` (dominates TSMOM in every window) |
| Crypto trend / diversifier | screening fail | `crypto_trend_study`, `crypto_diversifier_study` |
| Overnight drift, intraday ETF families, 1DTE translator | rejected / stood down | `intraday_strategy_study`, `intraday_1dte_shadow_launch` |
| Calendar: turn-of-month, pre-FOMC drift | rejected / deferred (effect decayed post-2015) | `turn_of_month_study`, `pre_fomc_drift_study` |
| Insider buying echo | reject, not worth trial (−2 bp captured vs 30 bp cost) | `insider_echo_study` |
| VIX term structure | reject, no value beyond realized vol | `vix_term_structure_study` |
| Liquid pairs | untestable at $10k | `liquid_pairs_study` |
| SPY option structures (bull put, bull call, fixed-width, put BWB, anticipatory hedge) | insufficient evidence — data, not idea | `*_spread_study`, `anticipatory_tail_hedge_study` |
| Earnings drift on a gap proxy; quality/accrual filters | deferred — data | `pead_data_audit`, `fundamental_momentum_filter_study` |

Two forward-only observations are still running and need no work: IWM
compression breakout (SPRT, `scripts/iwm_breakout_forward.py`, 5 trades,
`continue`) and the FOMC trend-off log.

## Candidates worth studying, in order

Every one of these must be pre-registered (objective class, bounds, cost
schedule, windows) before a result is looked at — `backtest/promotion.py`
for the class, `backtest/deployable_sim.py` for the simulator, and the
`benchmark_beater` objective against SPY unless the class is explicitly
`risk_reducer`. Expect most to fail; the bar is the point.

### 1. Volatility-managed SPY core

Scale the SPY core between ~0.5× and ~1.5× (SSO for the >1× part) on
trailing realized volatility (Moreira & Muir, 2017; the literature since
finds the out-of-sample gain is mostly smoother returns, not higher ones —
to *beat* SPY on CAGR it needs average leverage above 1×).

- Why first: fits the account (two ETFs, fractional, low turnover, no
  shorts), reuses `deployable_sim` unchanged, and it is the only candidate
  that settles an open operational question — the 2× profile's
  `volatility_overlay` went `active` on 2026-09-14 **with no M6-specific
  backtest** (`portfolio_mix_study.json` limitations: "Gross 2.0 rows …
  not the (now active) volatility overlay"). As of 2026-09-21 that overlay
  recommends ~0.94×, so the "2×" lab runs below base and tests nothing
  base does not. This study decides whether the overlay stays, changes
  target, or comes off.
- Own prior evidence: `vix_term_structure_study` (VIX adds nothing beyond
  realized vol) and the 2× de-risking table in
  [research.md](research.md#volatility-de-risking-overlay-2-shadow) (12%
  target: better Sharpe/drawdown, lower CAGR — a `risk_reducer` shape).
- Register two variants only: a 12%-target overlay on the whole M6 book
  at gross 2.0 (what the 2× lab is actually running), and a realized-vol
  scaled core at gross 1.0 judged `benchmark_beater`. No grid search.

### 2. News-conditioned earnings drift (PEAD)

The one stock-selection candidate the repo has already cleared for study:
`news_pead_feasibility.json` (`proceed_news_conditioned_event_study`,
8,606 single-symbol events, classifier hand-checked to 100% precision on a
120-headline sample). Its `next_study_contract` field already fixes the
signal inputs, the earliest entry (first completed 30-minute bar after
publication), the controls, and the prohibitions — implement that
contract, do not redesign it.

- Long-only is fine (whole-share shorting is the $10k account's binding
  constraint; do not build the short leg first).
- The failure to expect is the insider-echo one: a real published effect
  whose tradeable subset (price ≥ $5, ADV ≥ $3M, 09:51/12:35 fills, 15 bp)
  captures nothing. Simulate under the live gate's universe and fill times
  before believing any number.
- Data gap to close first: an independent, freshly drawn out-of-sample
  precision check of the headline classifier (flagged in
  `news_pead_feasibility_precision_check.json`, never done).

### 3. Robustness of the sleeve that passed

Not a search for a better M6 — a check on how fragile it is. Three
variations, registered together, judged as a battery:

- Dual confirmation: switch QLD off only when both QQQ and SPY are below
  their 200-day SMAs (fewer whipsaws in the years M6 trailed: 2010, 2011,
  2015, 2016).
- Risk-off leg by bond trend: BIL vs IEF chosen by IEF's own SMA, instead
  of always BIL.
- Sleeve weight: 0.20 is pinned by `risk.max_leveraged_exposure_pct`, not
  by evidence. Test 0.15 and 0.25 (the latter needs a config change and a
  new elevated-cap check — see AGENTS.md on SPY's position cap).

If all three land within the drawdown noise band and inside a few
hundredths of Sharpe, M6 is robust and this is done. If one variant is
dramatically better on 2023+, be suspicious — that window is spent.

### 4. Buy data before buying ideas

Seven studies are blocked on data, not on ideas. Two purchases would
unblock most of them:

- Historical option chains with Greeks (SPY at minimum): un-blocks the
  five option studies and the tail-hedge study, and lets the bull-put
  experiment be judged on more than 3 shadow observations.
- An earnings-estimates / actuals feed (or a fundamentals feed with
  coverage of the universe): un-blocks true PEAD and the quality filter.

This is plausibly better value than any single new strategy.

## Skip

Documented so the next person does not spend a day on them: more momentum
variants; GEM / DAA / BAA and other HAA cousins (same return source, same
2023+ shortfall); factor ETFs (MTUM already failed raw-vs-raw; quality and
low-vol trail SPY after fees over this sample); sector rotation (momentum
again); calendar effects beyond the two already tested; anything where
the 09:47 / 12:35 fill schedule is a material fraction of the edge.

## Operational items open on 2026-09-22

Not research, but they gate what the research can learn. Fixed in v1.4.4
(2026-09-21): the options close-order lifecycle, session counting, the
journal container's GitHub auth, stop exemption for stood-down sleeves.
Still open:

- **2× volatility overlay** — running below 1× on a realized-vol window
  dominated by the retired 2.3×-gross long/short book. Decide via
  candidate 1 above; until then the 2× lab is not a 2× test.
- **FBRX** — inactive/untradable at Alpaca, ~1 sh base / ~2.5 sh 2×,
  skipped every run. Needs a broker-side resolution, not code.
- **Dead experiments** — the four options shadows have zero observations
  because `shadows2x` is deliberately unscheduled (`deploy/crontab`,
  2026-08-17); the bull-put experiment can therefore never meet its own
  20-observation bar; the execution-timing experiment (25/100 matched
  fills) cannot complete at M6's trade frequency. Either schedule the
  shadow job as its own reviewed change or set the experiments `off` and
  close the timing registration.
- **Stuck options orders** — no alert exists for a structure that stays
  `closing_pending` across a session or an options order unfilled after
  30 minutes; the 2026-09-14 close sat unnoticed for a week.
- **Journal container** — first scheduled push not yet confirmed; after a
  `job=journal … pushed N commit(s) … end rc=0` block appears in
  `logs/paper-*.log`, remove the host crontab's `auto: journal` line
  (docs/operations.md).

## How to pick this up

1. Read AGENTS.md end to end, then
   [research.md](research.md#live-results-and-the-2026-09-14-stand-down).
2. Check the M6 validation status in the latest `reports/paper/weekly-*.md`
   before proposing anything — if a kill rule has fired, that decides the
   next step, not this list.
3. Write the registration JSON for the candidate first (copy the shape of
   `reports/absolute_return_campaign_registration.json`), commit it, then
   run the study. A result that arrives before its registration is not
   evidence.
4. Record the outcome as a `decision` in the study JSON and update the
   table above.
