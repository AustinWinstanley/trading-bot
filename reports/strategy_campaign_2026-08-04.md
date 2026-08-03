# Strategy research campaign — 2026-08-04

## Decision

No new checked-in strategy change. This campaign fixed the shared backtest
machinery (the cross-sectional panel and the promotion-gate logic every study
had reimplemented differently), re-baselined every headline number on the
corrected data, fixed a chronic live risk-gate bug that was capping SPY at a
quarter of its target exposure in both paper accounts, and re-audited eight
previously rejected or marginal studies against the corrected panel. All
eight rejections are confirmed — several on cleaner, more decisive evidence
than before, one (the option-hedge trio) relabeled from a flat rejection to
insufficient evidence, and one (liquid pairs) relabeled from rejected to
untestable at this account size.

A same-day follow-on (below, "Phase 4") built point-in-time fundamentals and
insider-transaction data from SEC's free bulk sources, fixed a real staleness
bug in the fundamentals pipeline, and ran the previously-deferred quality
filter and the accruals signal through the full promotion gate for the first
time. Neither is promoted. Quality is deferred again on insufficient data
coverage; accruals is rejected under the standard gate but passes a
purpose-appropriate risk-reduction gate cleanly in both held-out cells.

The one live-trading fix in this campaign — the SPY position-cap correction
— already shipped in its own reviewed commit ahead of this summary, since it
corrects a bug actively suppressing ~45-90 percentage points of intended
exposure in both paper accounts every day since 2026-07-24.

## Results

| Candidate | Prior decision | New decision | Most important result |
|---|---|---|---|
| Cross-sectional panel warm-up rows | (not previously identified) | **Fixed** | 14 rows before 2020-07-27 compressed multi-week returns into single "daily" observations, inflating early-window Sharpe ~19% and understating COVID drawdown 9.2pp. Dropping them is correct, but the corrected panel has *zero* coverage of any kind before 2020-07-27 — confirmed even for AAPL and MSFT — so `early_2020_2022` no longer contains a COVID-crash observation for any panel-dependent sleeve. |
| Trend/SPY gap-return convention | (not previously identified) | **Fixed** | Two sleeves of the same portfolio disagreed about how to treat the same data gap; unified via a shared resample helper. |
| Promotion-gate rule | 7 studies, 7 different implementations | **Unified** (`backtest/promotion.py`) | Three pre-registered objective classes (`return_enhancer`, `risk_reducer`, `cost_reducer`) so a candidate's declared purpose determines its bar instead of the writeup after the run. |
| `heldout_2023_plus` as a hold-out | Used uncritically | **Frozen** | ~15 studies have already used it as a promotion gate. 2026-08-04 onward is reserved as the real final-validation window. |
| SPY position cap (live bug) | Undetected | **Fixed** | `max_position_pct` (15%/30%) silently capped `equity_core`+`trend`'s combined 60%/120% target. Confirmed from the server journal: SPY rejected on ~19 of ~19 base opportunities and ~14 of ~14 on 2x since 2026-07-24. |
| IPO-age gate | Unreachable (`listed_days` hardcoded to 10,000) | **Fixed** | Now proxied from real bar history; verified against a real 2025 IPO (CRCL) landing on its actual listing date. |
| `short_panic_regime_study` (momentum-crash protection) | Reject — internally inconsistent (base passed, 2x rejected on a claim its own held-out data contradicted) | **Reject — confirmed, and resolved** | `panic_days=0` in every held-out cell proves the gate never fires there; the prior held-out "failure" was iteration noise, not an effect. On the corrected panel, base now also fails: the gate fires 30 times in `early_2020_2022` and produces *zero* measurable drawdown protection even then. Also clarified: this candidate targets market-wide crash rebounds, not the single-factor concentration pattern of the 2026-07-23 live loss — the two should not be conflated. |
| `cash_reserve_study` (idle-cash yield) | Reject (SHY, duration losses in early window) | **Reject (SHY) — but BIL (fetched 2026-08-03, free via Tiingo) is a near-miss** | BIL improves CAGR and Sharpe outright in every window; only fails max drawdown by 1 basis point in the early window. Confirms the original rejection was measuring instrument choice, not the underlying question. Worth re-checking on frozen fresh data rather than treating as settled. |
| `frontier_study` (marginal diversifiers) | Baseline was not the deployed portfolio (SPY+PUT+monthly-MOM_LS instead of SPY+TSMOM+TREND+weekly-MOM_LS) | **Fixed and re-run** | TSMOM's marginal Sharpe (~-0.02) is now expected — it's already 25% of the combo. SPY/QQQ-overnight still show the largest positive marginal Sharpe, but that number is cost-free and `overnight_cost_study.json` already found realistic break-even is ~2bps/leg; not new evidence for an overnight sleeve. |
| `momentum_buffer_study` (rank buffer) | Reject (Sharpe missed by 0.019–0.044, no cost sensitivity) | **Reject — but cost-elastic, quantified** | At ≥20bps assumed cost, the buffer passes cleanly in both profiles and both windows under a *stricter* zero-Sharpe-cost bar than the original. At the standard 15bps, only base/held-out narrowly misses (Sharpe 1.291→1.283). |
| `account_mandate_study` (defensive 2x mandate) | Reject (held-out missed an unexplained 90% CAGR floor by 2.9pp despite improving both risk metrics) | **Reject — confirmed under a purpose-built risk_reducer gate too** | Early window fails substantively (Sharpe -15%, drawdown improvement only 7% against a pre-declared 10% bound). Held-out misses a pre-declared 3.0pp CAGR-cost budget by 0.04pp. The mandate framing also overstates diversification — `capital_split_study` finds the household is effectively one 1.5x portfolio. |
| Three exact-contract option-hedge studies | Reject | **Relabeled `insufficient_evidence`** | 3–6 completed spreads against self-declared minima of 12–36; the two studies with a synthetic long-history arm price options at a median 1.43–1.55x the observed exact-contract debit, a systematic bias that invalidates their long-history rejection reasons. Not evidence against the hedges — evidence the sample is too small. |
| `liquid_pairs_study` | Reject (0.017 Sharpe difference) | **Relabeled `untestable_at_10k`** | 86 of the sample's days have a short leg that rounds to zero whole shares while the long leg fills normally — a one-legged position on those days, not the pre-registered market-neutral pair. A diagnostic fractional-shorts variant (not deployable — Alpaca cannot short ETFs fractionally) is included for reference. |
| `risk_overlay_study` (MOM_LS stop/re-entry removal) | Adopt — but the quantitative case rested on the (then-uncorrected) early window; held-out data mixed | **Adopt — confirmed on cleaner evidence** | Also fixed a gate bug (`any()` instead of `all()` over the 4 cells, and `max_dd` collected but not checked). On the corrected panel the live overlay now underperforms control on both Sharpe and CAGR in all 4 cells, not just the early window — the removal decision is unchanged but no longer rests on an ambiguous held-out picture. |

## New information worth retaining

### The corrected panel has no COVID-crash coverage, at all, ever

`state/xsec/close.parquet` has zero usable data before 2020-07-27 for any
symbol on the free Alpaca tier — not a handful of failed fetch batches, a
hard platform floor. Every `early_2020_2022` result for a panel-dependent
sleeve (MOM_LS, the retired clone sleeve) should be read as covering roughly
Aug 2020–Dec 2022 with a thin first year (MOM_LS itself doesn't go live
until 2021-08-25 on this panel), not the crash-inclusive window its label
implies. The only crash-era evidence for these strategies remains the
French-Mom long-history proxy, itself only 0.55–0.60 correlated with the
sleeve it stands in for. See `AGENTS.md`.

### A live risk-gate bug was suppressing real exposure in both paper accounts

Confirmed directly from the server journal, not inferred: SPY was rejected
at the 15%/30% position cap on essentially every opportunity for the ten
days before this campaign, realizing ~15%/~31% exposure against a 60%/120%
sleeve target. This is the same class of finding as the MOM_LS stop-loss
discovery — a control the backtest never modeled, silently describing a
different portfolio than the one deployed — except this one was actively
suppressing intended exposure rather than adding an untested control. Fixed
with a sleeve-scoped elevated cap, following the existing stop-exemption
precedent, and shipped separately from this summary.

### Marginal near-misses cluster right at the standard cost/threshold assumptions

Three separate re-audits this round (BIL cash reserve, the momentum rank
buffer, the account mandate) turned out to hinge on assumptions sitting
exactly at or just past the repo's standard conventions — one basis point of
drawdown, 5bps above the standard turnover-cost assumption, 0.04
percentage points past a pre-declared CAGR budget. None of these clear the
bar as pre-registered, and none should be promoted on this evidence. But
treating "rejected under the standard assumption" as equivalent to "rejected
robustly" would be a mistake for exactly these three — they are the
strongest candidates to re-examine once the frozen 2026-08-04-onward
validation window has accumulated enough data.

### `heldout_2023_plus` is retired as a clean hold-out

Documented in `AGENTS.md`. Roughly fifteen studies have already used it to
arbitrate a promotion decision; 2026-08-04 onward is reserved as the real
final-validation window going forward.

## Limitations

Unchanged from the rest of this sleeve's research: historical easy-to-borrow
and locate availability are unavailable, and the cross-sectional universe is
survivorship-biased, so positive MOM_LS results remain optimistic upper
bounds. Two measurement questions remain open and unresolved by this
campaign, documented in `AGENTS.md` rather than fixed: risk-free rate is
modeled as zero everywhere (biasing cross-window absolute Sharpe
comparisons), and `production_portfolio.py`'s idealized-fractional MOM_LS
short-borrow cost disagrees with `short_capacity_study.py`'s
realized-whole-share cost — the headline README numbers use the idealized
model, not the realistic capacity-constrained one.

## Operational findings from the same review

- **The server's `state/paper.db` / `state/paper_2x.db` were pulled into
  this checkout for the SPY-cap investigation** and are now present locally
  (gitignored). They directly confirmed the position-cap bug's live impact
  before any code change was made.
- **`backtest_2010.json`, `sweep_concentration.json`, `earnings_llm.json`,
  and `fund_signals.json` were marked `unreliable`** with reasons, rather
  than deleted — a superseded legacy engine, an empty result set, and a
  look-ahead-contaminated point-in-time construction respectively.
  `fund_signals.json`'s marking is superseded by the Phase 4 fix below; its
  current version is a fresh, corrected run, not the flagged one.

## Phase 4 — point-in-time fundamentals, quality filter, accruals

The 2026-07-23 campaign deferred a point-in-time quality filter because its
source SEC parquet was absent and a reproducibility audit found the code that
had produced the earlier `fund_signals.json` numbers used `filed_date`
incorrectly, a current (non-point-in-time) CIK-to-symbol map, and annual
flow facts instead of true trailing four quarters
(`reports/quality_momentum_filter_feasibility.json`).

**The fundamentals pipeline already existed** (`engine/fundamentals.py`),
correctly keyed on the SEC acceptance (`filed`) date, not period end — the
audit's `filed_date` concern turned out to describe a *different*, no-longer-
present script, not this one. What was real: `backtest/fund_signals.py`'s
trailing-12-month flow calculation only ever used the annual (qtrs=4) 10-K
figure, so net income/OCF/gross profit sat stale for up to a year between
10-Ks despite fresher 10-Q facts sitting in the same cache. Fixed with a
proper rolling 4-quarter sum, falling back to the annual figure only before
four quarterly filings have accumulated.

Built point-in-time fundamentals (2019–2026, 4.29M facts) and insider
transactions (2019–2026, 2.53M rows) from SEC's free bulk data sets to run
this. Investigating the CIK-to-symbol map surfaced a bigger problem than
expected: `engine.fundamentals.cik_map()` is current-listing only and drops
**45.5%** of CIKs with fundamental facts (4,308 of 9,459) — not a long tail
of tiny filers; it drops known large delisted names (verified on Twitter/
TWTR) too. A proposed quick fix (SEC's per-company submissions API) does not
work: verified empirically that the API returns an empty ticker field for
every deregistered company, and the company-facts API only carries numeric
facts, not the text ticker tag. There is no clean bulk source for a delisted
company's historical ticker; recovering it would mean parsing individual
filings' XBRL cover pages one at a time. Documented rather than built —
every run now reports the exact coverage number
(`cik_map_coverage`/`coverage_by_variant` in the report JSONs) so this can't
silently drift stale, and results are read the same way this repo already
reads the price panel's own survivorship bias: a rejection is trustworthy, a
positive result is an unvalidated upper bound.

### Quality filter — deferred again, on coverage this time

Ran the feasibility contract's exact pre-registered design (remove bottom-
quality-quintile names from MOM_LS longs, top-quintile from shorts, continue
down the unchanged momentum rank) through `xsec_momentum.build_portfolio`,
reproduced to `0.00e+00` against the unfiltered control. Coverage came in at
**35.2%**, far short of the contract's 80% requirement — quality needs both
gross profit and operating cash flow simultaneously, compounding the 45.5%
CIK-map gap. Correctly deferred rather than judged on an underpowered
sample; the underlying data contract's coverage requirement did its job.

### Accruals — rejected under the standard gate, but a real risk/return split

`reports/fund_signals.json` (fixed pipeline) confirms the 2026-07-23 audit's
flagged finding: accruals is the standout fundamental signal — Sharpe 0.313
standalone, the only sign-stable signal across both halves (0.500 / 0.348),
near-zero momentum correlation (0.035), and a genuine diversification
benefit combined with momentum (0.748 → 0.800 Sharpe).

Run through the same MOM_LS filter contract as quality, coverage clears
**80.6%**. Under the standard `return_enhancer` gate it is rejected: Sharpe
and CAGR are both modestly lower than control in all 4 cells. But the
feasibility contract's own hypothesis frames this as a risk reducer ("may
reduce momentum crashes and distress exposure without replacing the momentum
rank"), so it was also evaluated under `backtest.promotion`'s `risk_reducer`
class with bounds fixed before running (≤1.5pp CAGR cost for ≥5% relative
drawdown improvement, a smaller budget than `account_mandate_study.json`'s
3.0pp/10% since this is a milder adjustment than a dedicated defensive
mandate): it **passes cleanly in both held-out cells** and fails in both
early-window cells. Rejected under the primary gate — the split is reported
rather than smoothed into either a clean pass or a clean fail.

### Not yet built

Sector-neutral momentum (dated SIC codes) and true PEAD (8-K acceptance
timestamps) remain deferred. Given the CIK-map investigation's outcome —
a proposed quick fix that didn't survive first contact with SEC's actual
API behavior — their feasibility should be verified before committing to
either, rather than assuming a clean bulk source exists.
