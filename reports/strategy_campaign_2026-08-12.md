# Strategy campaign — 2026-08-12

## Imported paper-journal evidence

The SQLite journals, not the overwritten early daily reports, are the source
for this review. Both accounts are healthy and have observations through
2026-08-12.

| Measure | Base | 2× |
|---|---:|---:|
| Latest imported equity | $10,164.78 | $10,281.87 |
| Return from $10,000 start | +1.65% | +2.82% |
| 2026-08-04 onward, latest-to-latest | +0.77% | +1.69% |
| Average target long / short / gross | 100% / 15% / 115% | 200% / 30% / 230% |
| Average actual long / short / gross | 94.18% / 6.13% / 100.30% | 187.44% / 20.79% / 208.23% |

The largest repeatable divergence is still MOM_LS short capacity. Since the
frozen-paper period began, whole-share rounding rejected 132 base proposals
($9,795 requested) and 72 2× proposals ($7,395 requested). Alpaca does not
accept fractional short orders, so this is a capital-size constraint rather
than a transient broker error.

The no-averaging gate also rejected 22 base MOM_LS target restorations and 37
2× restorations. These were investigated below instead of being assumed to be
either helpful or harmful.

FXE repeatedly fell below the live Alpaca/IEX $1m liquidity floor, leaving an
approximately 3.54 percentage-point base target gap and 7.08-point 2× gap
while its TSMOM signal was active.

## Completed decisions

### Systematic target restoration — reject

Allowing a losing MOM_LS incumbent to increase only back to its unchanged
systematic target raised screening-window Sharpe and CAGR in all four cells.
It nevertheless worsened max drawdown in three cells, by 0.37–0.60 percentage
points, and therefore failed the pre-registered return-enhancer gate. The
global no-averaging rule remains unchanged.

See `reports/target_restoration_study.json`.

### Capacity-matched long exposure — reject

Matching long targets to attainable whole-share short dollars corrected the
base sleeve from approximately 11.9% long / 7.4% short to 7.3% / 7.4%. It
improved all early-window metrics, but in 2023+ it cost 3.02 CAGR points in
base and 2.32 points in 2×; the 2× drawdown improvement was only 3.76% against
the pre-declared 5% minimum. The current unmatched construction remains.

This result is evidence against silently shrinking longs merely to make the
sleeve look neutral. It does not solve short capacity; larger account equity
or a different instrument construction may still do so.

See `reports/capacity_matched_momentum_study.json`.

### TSMOM liquidity alignment — implement without redistribution

A candidate that removed sub-$1m assets before inverse-vol normalization had
no historical events in the Tiingo consolidated-volume cache and therefore
could not pass the promotion gate. Live Alpaca/IEX data does show the issue.

Production now applies the existing live floor before proposing an order but
*after* TSMOM weights are normalized. The untradeable allocation remains cash,
exactly as it did after the risk-gate rejection; other targets are not enlarged.
The risk gate independently rechecks liquidity at execution time. This removes
doomed repeat proposals without inventing a return claim or weakening the
control.

See `reports/tsmom_liquidity_alignment_study.json`.

### Whole-share-aware MOM_LS breadth — reject

Concentrating both sides from twenty to ten names materially improved
deployability: base zero-share short targets fell from 24.84% to 5.01%, and
average realized base short gross increased from 7.41% to 9.85%. The added
concentration nevertheless reduced Sharpe in all four screening cells and
worsened early-window maximum drawdown by 1.52 percentage points in base and
4.04 points in 2×. Five- and fifteen-name sensitivity checks also failed to
dominate the twenty-name control.

The result separates execution capacity from expected return: larger slots do
recover intended short exposure, but the narrower signal is not a better
portfolio. Production remains at twenty names per side.

See `reports/momentum_breadth_study.json`.

## Next pre-registered experiment: execution timing

Matched 2026-08-04 onward MOM_LS fills show approximately 0 bp adverse
slippage in base and +7.82 bp in 2× across 31 same-symbol/same-side pairs. The
accounts currently begin their morning runs four minutes apart (09:47 ET and
09:51 ET), so this difference may be timing rather than leverage or market
impact. The sample is too small and selected to justify changing cron now.

Before the next schedule change, freeze this experiment:

1. Control: retain the current four-minute separation while collecting at
   least 100 matched fills.
2. Candidate: move 2× to one minute after base for a fixed 20-session paper
   window; do not run both at the exact same minute, so local/API contention
   does not become a new confounder.
3. Primary metric: notional-weighted adverse slippage difference on matched
   symbol, side, and target-generation date.
4. Promotion bar: at least 3 bp improvement for 2×, no more than 1 bp
   degradation in base, no increase in submission failures, and no overlapping
   run/lock failures.
5. Frozen result: decide after the window; do not shorten it because an early
   result looks favorable.

A sustained 3–8 bp improvement matters despite sounding small: applied to
several one-way turns per year, it is plausibly worth tenths of a percentage
point of annual return without adding market risk. This is an estimate, not a
backtest result, until turnover and the timing experiment are both observed.

## Operational verification

- Full test suite passes.
- Both mutation-free daily runs pass.
- Both paper-account health checks report `HEALTHY`.
- The local checkout lacks the server-generated weekly MOM_LS target file, so
  local dry runs correctly treated that sleeve as stale and proposed closing
  it. Those proposals were not submitted and are not a server recommendation.
- Imported `state/paper*.db.bk` files remain local and untracked.
