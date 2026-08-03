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
