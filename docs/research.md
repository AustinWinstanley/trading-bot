# Research record and current estimates

This page is the detailed backtest evidence behind the strategy summarized in
the [README](../README.md). It reports transaction-cost-inclusive estimates
over a real, short, post-2020-dominated sample — not forecasts, and not fully
deployable $10,000-account results in every table below (several explicitly
model an idealized fractional-share capacity; the deltas from realistic
whole-share constraints are quantified in their own section).

See [`AGENTS.md`](../AGENTS.md) for the research conventions (the promotion
gate, the frozen validation window, cost assumptions) that produced every
number here, and `reports/*.json` for the underlying study with its
pre-registered `decision` field. **Existing decisions are binding** — check
for one before proposing something that looks similar; reopening a decision
needs new evidence, not a fresh opinion.

## Headline portfolio estimate

Transaction costs, 3% short borrow, and 5% margin financing included, over
2020-07-28 through 2026-07-22:

| Portfolio | CAGR | Sharpe | Max drawdown |
| --- | ---: | ---: | ---: |
| Base SPY-core profile | 13.46% | 1.129 | -13.83% |
| 2× SPY-core profile | 20.77% | 0.918 | -26.82% |
| SPY buy and hold | 16.64% | 1.003 | -24.50% |

These are idealized-capacity estimates, not forecasts. MOM_LS assumes
fractional shorts and a constant target short-borrow charge; Alpaca requires
whole-share shorts, so the real paper accounts achieve less short exposure —
quantified in "Whole-share short capacity" below. The individual-stock
universe also contains only currently listed companies and is
survivorship-biased. The headline results are therefore an optimistic upper
bound, over a short sample dominated by the post-2020 market.

## The sample has no COVID-crash observation

**The window starts 2020-07-28, not 2020-02-13, and contains no COVID-crash
observation.** The cross-sectional stock panel behind MOM_LS has no usable
data before that date for any symbol — it isn't a sampling choice, it's where
the underlying free-tier data begins. An earlier version of this table used a
panel with a handful of pre-2020-07-27 rows that each compressed several
weeks of return into a single "daily" observation, which understated
drawdown and overstated Sharpe; those rows are now dropped rather than
trusted. See `AGENTS.md` ("The cross-sectional panel has no COVID-crash
coverage") for exact bar counts and what this means for the `early_2020_2022`
window specifically.

## Production must match what the backtest models

**These numbers only describe what is deployed if production does not add
untested behaviour on top.** `backtest/production_portfolio.py` models MOM_LS as
a pure weekly rebalance — no stop-loss, no re-entry block. Production ran both
from launch until 2026-08-03, so the deployed strategy was measurably not the
one estimated here. `backtest/risk_overlay_study.py` quantified the gap and the
controls were removed from that sleeve. Before adding any risk control to a
sleeve, check whether the backtest models it; if not, the table above stops
describing reality.

## Delisting sensitivity

A Tiingo-metadata/Alpaca-bars sensitivity test added 1,481 delisted listings.
The observed-last-price case and a deliberately severe case that assigns every
delisting a total loss bound the production estimate:

| Delisting assumption | Base CAGR | Base Sharpe | 2× CAGR | 2× Sharpe |
| --- | ---: | ---: | ---: | ---: |
| Observed last price | 13.32% | 1.118 | 20.47% | 0.907 |
| Every delisting loses 100% | 12.33% | 1.042 | 18.36% | 0.832 |

MOM_LS remains positive across those bounds, but historical borrowability and
actual delisting proceeds are unavailable, so this reduces rather than removes
the uncertainty.

## Whole-share short capacity

A whole-share capacity study modeled the actual $10,000 account sizes.
The base account realizes only 9.18% average short exposure versus its 15%
target because 25.0% of ranked shorts round below one share. The 2× account
realizes 25.04% versus 30%, with 3.9% rounding to zero:

| Construction | Base short | Base CAGR / Sharpe | 2× short | 2× CAGR / Sharpe |
| --- | ---: | ---: | ---: | ---: |
| Fractional benchmark | 15.00% | 13.56% / 1.136 | 30.00% | 20.97% / 0.925 |
| Current whole-share bottom 20 | 9.18% | 13.09% / 1.078 | 25.21% | 20.42% / 0.901 |
| Price-aware bottom 20 | 12.15% | 12.93% / 1.084 | 26.11% | 20.36% / 0.900 |
| Whole-share bottom 10 | 12.30% | 13.33% / 1.094 | 27.59% | 19.70% / 0.865 |

Neither alternative dominates across the early and held-out windows, so the
current construction remains deployed while real paper fills accumulate.
Historical easy-to-borrow status is unavailable, making all variants optimistic
execution bounds.

A production-fidelity follow-up concentrated both sides of MOM_LS to create
larger whole-share slots. Ten names per side reduced base zero-share short
targets from 24.84% to 5.01% and raised average realized base short gross from
7.41% to 9.85%. That additional exposure did not improve the portfolio: Sharpe
fell in all four profile/window cells, while early-window maximum drawdown
worsened from -12.54% to -14.06% in base and from -22.99% to -27.03% in 2×.
Five- and fifteen-name sensitivity checks also failed to dominate. Production
therefore remains at twenty names per side; fixing capacity by concentrating
the signal introduces more risk than the recovered short exposure earns.

**These two paragraphs report two different numbers for the same nominal
quantity** (base short gross for the deployed twenty-name construction):
9.18% above from `short_capacity_study.py`, 7.41% here from
`backtest/deployable_momentum.py` (a separate simulator, built later).
`deployable_momentum.py` has not been checked to reproduce
`short_capacity_study.py`'s control, which AGENTS.md's own research
convention requires before trusting a reimplementation's variants — treat
7.41%/9.85% as unreconciled against the figures above until that check
exists. Neither backtest number matches the live paper journals either: the
base account's actual realized short gross was 6.13% as of 2026-08-12,
lower than both estimates. All three are measuring related but distinct
things (two backtest constructions over different windows, one realized
figure from actual fills), and the gap between them is itself informative —
whole-share rounding at $10k equity is evidently costlier in practice than
either backtest fully captures.

## Bootstrap ranges

A 5,000-path, 63-session block bootstrap puts useful ranges around those point
estimates. With the deliberately severe universal-zero delisting drag applied:

| Profile / horizon | CAGR p05 | Median | CAGR p95 | Chance of loss | Drawdown p05 |
| --- | ---: | ---: | ---: | ---: | ---: |
| Base, 1 year | -4.9% | 12.4% | 31.3% | 12.0% | -15.8% |
| Base, 3 years annualized | 2.1% | 12.2% | 23.2% | 2.3% | -20.4% |
| 2×, 1 year | -14.0% | 19.2% | 59.9% | 18.2% | -30.0% |
| 2×, 3 years annualized | -1.1% | 18.6% | 41.6% | 6.2% | -39.0% |

These are conditional scenario ranges, not calibrated forecast probabilities.
Resampling cannot create crises or regimes absent from the short 2020–2026
source history, so capital planning should allow outcomes worse than p05. A
21/63/126-session block sensitivity moved the three-year p05 CAGR to 1.0%–2.9%
for base and -3.5% to 0.2% for 2×; the 2× drawdown p05 reached -43.7%.

## Long-history stress proxy

A separate 2007–2026 stress proxy reconstructs the SPY, trend, and 15-asset
TSMOM sleeves and substitutes the point-in-time
[Kenneth French daily momentum factor](https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/Data_Library/det_mom_factor_daily.html)
for unavailable pre-2020 stock ranks:

| Long-history proxy | CAGR | Sharpe | Max drawdown | GFC return | COVID-crash return |
| --- | ---: | ---: | ---: | ---: | ---: |
| Base | 7.80% | 0.785 | -26.25% | -15.60% | -16.50% |
| 2× | 9.22% | 0.530 | -50.30% | -34.11% | -31.25% |

The proxy's 2020–2026 correlation to the actual capacity-adjusted MOM_LS
contribution is only 0.55–0.60. These are stress estimates, not a reconstructed
production backtest, but the limited incremental return and much larger
drawdown keep 2× strictly experimental.

## Combining the two paper accounts

The two $10,000 paper accounts should be judged as one $20,000 portfolio.
Because they trade the same sleeves, an equal-dollar split is approximately a
1.5× portfolio rather than a diversified pair:

| Capital in 2× account | Nominal combined leverage | Recent CAGR / max drawdown | 2007–2026 proxy CAGR / max drawdown |
| ---: | ---: | ---: | ---: |
| 0% | 1.00× | 13.09% / -14.62% | 7.81% / -26.49% |
| 25% | 1.25× | 15.03% / -17.87% | 8.28% / -33.34% |
| 50% (current paper split) | 1.50× | 16.90% / -21.03% | 8.67% / -39.62% |
| 75% | 1.75× | 18.70% / -24.09% | 8.99% / -45.38% |
| 100% | 2.00× | 20.42% / -27.09% | 9.24% / -50.63% |

For the current equal split, the severe recent-history bootstrap estimates a
15.5% chance of a negative one-year CAGR and a 46.1% chance of a drawdown over
20% during a three-year path. The long-history proxy lost 25.3% through the
GFC window and 24.1% during the COVID crash, with drawdowns of 30.3% and 24.4%
respectively. These estimates support keeping the equal split as an aggressive
paper experiment, while treating its combined risk budget and drawdown as
1.5× exposure.

## Live results and the 2026-09-14 stand-down

Seven weeks of live paper trading (2026-07-23 → 2026-09-14, 36 sessions)
were decomposed symbol-by-symbol, run-by-run from the server journals
(`snapshots.positions` marks plus `orders` fills, half-open windows because
a snapshot is written before that run's fills). The decomposition
reconciles to the penny against the snapshot equity change:

| Bucket | Base | 2× |
| --- | ---: | ---: |
| SPY core (`equity_core`+`trend`) | +$46 | +$81 |
| TSMOM | +$2 | +$4 |
| MOM_LS long book | −$101 | −$360 |
| MOM_LS short book | −$43 | −$305 |
| Options experiment | $0 | $0 |
| **Total equity change** | **−$96 (−0.98%)** | **−$565 (−5.50%)** |

SPY rose +3.23% over the identical dates (marks taken from the same
snapshots). Execution friction was not the cause: notional-weighted adverse
slippage totalled $5 (base) and $25 (2×) over the whole run.

MOM_LS never ran market-neutral. At a ~$75 slot roughly half of its shorts
round below one whole share (base realized 42–63% of its short target,
mean net exposure +7%), so the sleeve was a levered long bet on the most
volatile 20 names in the market with a partial hedge. On 2×, where the
shorts did fill (85%), the short book lost −$305 on its own — dominated by
high-short-interest names that squeezed (BMNR, MSTR, RBLX, TEAM).

A leg decomposition on the repo's own panel (control reproduces
`production_portfolio.json`'s sleeve correlations to four decimals) shows
why: the short leg's entire value was 2022 (+58%) and it lost −44% / −6% /
−36% in 2023 / 2024 / 2025, while the long leg carried the held-out window
(41.8% CAGR, Sharpe 1.00, 0.65 beta, 44% vol). The deployed sleeve was a
2022 hedge being paid for every year since.

Decision (2026-09-14): `paper_portfolio.sleeves.mom_ls` set to 0.0 in both
profiles; its 15% folded into `equity_core` (0.40 → 0.55) with
`risk.elevated_position_pct` raised to cover the 75% combined SPY target.
The 2× lab's volatility overlay was promoted from `shadow` to `active`
(35 observations, recommending 1.11× against 2.00× applied). Replacement
candidates — vol-scaled long-only momentum (and MTUM), a trend-filtered
leveraged-index sleeve, and Keller's HAA — are to be judged under a new
`benchmark_beater` objective (must beat SPY's CAGR with drawdown inside the
paired-bootstrap noise band) through a live-gate-faithful $10k simulator
before any of them goes live. Prior `decision` fields on momentum-sleeve
variants are superseded by this stand-down, not by a fresh opinion: the
sleeve's live realization diverged from all three of its own simulators
and the design objective (Sharpe with zero-tolerance drawdown) was never
"beat the market".

## The absolute-return campaign (2026-09-14) and the M6 portfolio

Pre-registered in `reports/absolute_return_campaign_registration.json`
before any candidate was run: three candidate sleeves, six fixed mixes, a
new `benchmark_beater` objective (beat SPY's CAGR, excess Sharpe not lower,
max drawdown within `paired_drawdown_noise_pp`, judged per cell in full +
`heldout_2023_plus` + `early_2020_2022`, stress windows on drawdown only),
every candidate pushed through `backtest/deployable_sim.py` — the
live-gate-faithful $10k simulator that reproduces `deployable_momentum`
to 1e-17 in compatibility mode — with `rf` from BIL. 2026-08-13 onward
was never read.

| Candidate | Standalone verdict | Why |
| --- | --- | --- |
| Trend-filtered leveraged index (`trend_leveraged_index_study.json`) | screened | Standalone fails 2023+ on excess Sharpe (bull-market whipsaws); inside an 80/20 SPY mix, QQQ/QLD clears every cell |
| Vol-scaled long-only momentum (`long_only_momentum_study.json`) | rejected | 10.6% CAGR / 0.45 excess Sharpe vs SPY 12.5% / 0.57 from 2021-08-26; unscaled reaches 34.5% in 2023+ at −29.9% DD; MTUM fails raw-vs-raw |
| Keller HAA (`haa_study.json`) | rejected | Superb drawdowns (GFC −12.5% vs −55.2%) but 10.6% vs 23.1% in 2023+; dominates TSMOM in every window (11.8% vs 2.5% CAGR) |

Mixes (`portfolio_mix_study.json`, base gross 1.0): every mix containing the
stock-momentum sleeve fails `early_2020_2022`; every SPY/SSO mix fails
2023+ on excess Sharpe; M2 (0.55 SPY + 0.20 QLD-trend + 0.25 HAA) misses
2023+ by 0.08 Sharpe. **M6 [QQQ/QLD] — 0.80 SPY + 0.20 (QLD while QQQ >
200-DMA, else BIL) — is the only passer**:

| Cell | M6 | SPY |
| --- | ---: | ---: |
| Full 2006-06-21 → 2026-08-12 | 13.9% / 0.70 / −49.9% | 11.5% / 0.59 / −55.2% |
| early_2020_2022 | 12.4% / 0.68 / −23.7% | 8.9% / 0.51 / −24.5% |
| heldout_2023_plus | 26.3% / 1.18 / −17.8% | 23.1% / 1.16 / −18.8% |
| GFC (drawdown only) | −49.9% | −55.2% (inside band) |

(CAGR / excess Sharpe / max drawdown.) Ahead of SPY in 16 of 21 calendar
years; behind in 2010, 2011, 2015, 2016 and 2006 (whipsaw years). Realized
vol 19.5%, one-way turnover 1.7×/yr. The shipped 2026-09-14 baseline
(0.55 SPY / 0.25 TSMOM / 0.20 trend, idealised streams) is 12.7% vs SPY
16.6% over 2020-07 → 2026-08, confirming the old design trails the market.

**Read this honestly.** M6 is leveraged index beta with a trend filter: a
20% slot in a daily-reset 2× ETF. Its edge is +2.4 pp/yr over the full
sample with a *thin* 2023+ risk-adjusted margin (0.02 Sharpe), and it
carries SPY-like drawdowns. Over a sample dominated by the 2023–26 bull
market, nothing else registered beat SPY on both return and risk-adjusted
return — that is a finding about the bar, not just about the candidates.
Promotion to real money is not on the table; M6 went live on paper under
`reports/absolute_return_paper_validation_registration.json` (kill rules
K1–K4, 63-session primary window, 126-session extension).

## Rejected and deferred candidates

### Volatility de-risking overlay (2× shadow)

A volatility de-risking overlay on the long-history 2× proxy updates weekly,
never exceeds 2×, and charges 5 bp per estimated portfolio-wide rescale:

| 2× risk policy | CAGR | Sharpe | Max drawdown | Average leverage |
| --- | ---: | ---: | ---: | ---: |
| Fixed 2× | 9.22% | 0.530 | -50.30% | 2.00× |
| 12% volatility target | 7.54% | 0.631 | -36.68% | 1.35× |
| 15% volatility target | 8.08% | 0.580 | -43.63% | 1.61× |
| 18% volatility target | 8.45% | 0.554 | -46.86% | 1.78× |

The pre-selected 15% variant failed its 25%-drawdown-reduction rule. The 12%
variant passed that same screen in both 2007–2016 and 2017+ and also improved
Sharpe/drawdown in both windows of the exact 2020–2026 production study. It is
therefore a candidate for a new paper-only test, not evidence for real-money
deployment. It currently runs as a read-only shadow recommendation on the 2×
profile; see [paper attribution](paper-attribution.md).

### 13F clone sleeve — removed

The former 13F clone sleeve was removed after a timezone-mixed forward-fill
bug was found. Correct point-in-time results reduced its estimated conviction
variant from 27.3% to 5.98% CAGR. Because nothing live reads 13F data any
more, the Sunday job no longer refreshes the holdings or CUSIP map unless
`paper_portfolio.sleeves.clone` is greater than zero — it was rebuilding the
map from ~36 SEC downloads weekly, leaving `state/thirteenf/cusip_map.parquet`
permanently dirty and able to raise a `CRITICAL` about a sleeve that is not
trading. `backtest/clone_study.py` and `backtest/production_portfolio.py` read
the committed snapshot; pass `refresh=True` if a study needs current data.

### Overnight equity replacement — rejected

A cost-aware overnight-equity study also rejected replacing the SPY core.
Close-to-open SPY broke even at only 1.91 bp per execution in the post-2013
sample. QQQ survived 2 bp standalone, but every tested SPY/QQQ overnight
replacement reduced production-portfolio Sharpe in the held-out window.

### Defensive rotation of the trend sleeve — rejected

A defensive-rotation study also rejected investing the 20% trend sleeve while
its SPY signal is off. The pre-selected 12-month relative-momentum rule rotated
among GLD, TLT, IEF, and cash using prior-day data and paid 8 bp per unit of
turnover. In the 2007–2016 design window it reduced proxy CAGR from 4.76% to
4.12%, Sharpe from 0.527 to 0.453, and worsened drawdown from -26.25% to
-31.66%. Its apparent improvement in the recent held-out window therefore did
not qualify it for a paper shadow. Fixed gold looked attractive recently but
worsened long-history held-out drawdown, so it remains an exploratory
observation rather than a selected strategy.

### Sleeve-specific volatility overlay on MOM_LS — rejected

A sleeve-specific volatility overlay produced a mixed result. The pre-selected
15% target scaled only MOM_LS, weekly, between 25% and 100% of its fixed
allocation. It improved the base long-history proxy from 7.80% to 8.52% CAGR
and reduced full-period drawdown from -26.24% to -21.43%, including turning the
2009 momentum-crash rebound proxy from -5.5% to +5.9%. But it worsened the GFC
return from -15.6% to -18.5%, did not improve 2017+ maximum drawdown, and
reduced exact 2020–2022 Sharpe from 0.615 to 0.589. It therefore failed its
pre-specified cross-window rule and is not promoted to paper shadow.

### Pilot/follower confirmation delay — rejected

A $5,000 pilot / $10,000 2× follower simulation tested whether the larger
account should wait for individual pilot MOM_LS positions to turn
directionally positive. The pre-selected one-session rule retained only 35.4%
of follower momentum exposure, reduced full-sample CAGR from 18.95% to 13.49%,
and worsened drawdown from -26.23% to -32.14%. A five-session variant improved
2023+ results but failed badly in 2020–2022. Delaying every trade also failed,
showing that neither confirmation nor delay was robust. No paper feature flag
was added; the result does not rule out strategy-level allocation after a
pilot accumulates many independent experimental trades.

### Options strategies — not currently traded

Options are not currently traded by the equity portfolio (a separate,
capped 2× options experiment described in
[paper attribution](paper-attribution.md) is a distinct, later addition). The
original options study rejected only passive cash-secured put writing and
fully covered calls. A broader Cboe benchmark screen subsequently tested
protective puts, collars, put-spread collars, iron condors, iron butterflies,
partial covered calls, and alternative put-write schedules as 20% replacements
for the SPY core.

No strategy passed its pre-specified cross-window promotion rule. Protection
was nevertheless meaningfully different from premium selling: a 20% CLL
collar replacement reduced the 2007–2026 proxy drawdown from -26.25% to
-14.73%, while CAGR declined from 7.80% to 7.03%. A 20% PPUT protective-put
replacement improved 2017+ Sharpe from 1.020 to 1.050 and drawdown from
-16.70% to -11.99%, but slightly reduced 2007–2016 Sharpe. Passive condors,
butterflies, and alternative put-writing variants did not improve robustly.
These are hypothetical SPX benchmark results, not simulated SPY option fills.

An exact-contract follow-up used Alpaca's expired SPY contract metadata and
daily option trade bars from February 2024 onward. The pre-selected rule bought
one 45-DTE 95%/90% put spread on the first session of a month when SPY was below
its 200-day average or 20-session realized volatility exceeded 20%. It included
$0.10 per leg of entry and exit friction and applied option dollar P&L to the
capacity-adjusted $10,000 2× account:

| 2024-02 through 2026-06 | CAGR | Sharpe | Max drawdown |
| --- | ---: | ---: | ---: |
| No hedge | 41.23% | 1.398 | -26.23% |
| Always-on 95/90 spread | 35.54% | 1.352 | -17.11% |
| Conditional 95/90 spread | 37.90% | 1.378 | -20.58% |
| Conditional 90/85 sensitivity | 39.57% | 1.409 | -22.02% |

The selected rule failed its promotion gate. Only 3 of 29 monthly observations
triggered, none occurred in the 2024 design window, and all three completed
spreads lost money. A single 95/90 contract put 3.50%–3.92% of starting account
equity at risk after modeled friction, exceeding the tested 1%–3% budgets. The
exploratory 90/85 spreads fit a 3% budget and nearly preserved Sharpe, but they
used the same three events and cannot independently validate the idea.
Always-on protection reduced drawdown but paid for it with lower return and
Sharpe. No options paper feature was added.

The contract study remains an execution approximation: expired-contract
metadata does not reconstruct exactly when every strike was first listed,
daily trades are not executable bid/ask quotes, and American assignment is not
replayed. Standard monthly expirations and $5 strike increments reduce, but do
not eliminate, that historical-chain uncertainty.

A second exact-contract study tested buying the cheaper 90%/85% spread before
stress rather than after it. Its rule entered only while SPY was above its
200-day average and 20-session realized volatility was below 15%, with maximum
loss capped at 2% per trade and 4% per calendar year. It also failed:

| Anticipatory hedge test | CAGR | Sharpe | Max drawdown |
| --- | ---: | ---: | ---: |
| Exact 2024-02–2026-06, no hedge | 41.23% | 1.398 | -26.23% |
| Exact anticipatory 90/85 | 39.48% | 1.368 | -25.94% |
| 2007–2026 2× proxy, no hedge | 9.22% | 0.530 | -50.30% |
| 2007–2026 synthetic anticipatory 90/85 | 6.32% | 0.404 | -50.30% |

Only 6 exact spreads completed, versus the predeclared minimum of 12. The
synthetic hedge improved the 2017+ drawdown from -31.56% to -28.82% and the
COVID crash from -31.25% to -28.51%, but it did not protect the GFC, 2011,
2015–2016, or 2022 windows because the calm signal and short holding period
were not aligned with those losses. Even a 1% annual premium budget reduced
the long-history proxy CAGR from 9.22% to 8.50% without improving its -50.30%
maximum drawdown; 2%, 4%, and 6% budgets progressively worsened CAGR.

The synthetic pricing model was intentionally conservative and priced the six
comparable exact entries at a median 1.43 times their observed trade-bar debit.
An optimistic flat-VIX pricing sensitivity still produced only 6.27% CAGR and
0.400 Sharpe. This makes the rejection insensitive to the chosen downside-skew
assumption, but not a fill-quality validation. No anticipatory options feature
was added.

A defined-risk bullish study then tested a 60-DTE 105%/110% SPY call spread as
a convex return satellite. The pre-selected rule entered monthly only while
SPY was above its 200-day average and 20-session realized volatility was below
20%, risking at most 4% per trade and 8% per calendar year:

| Bull call-spread test | CAGR | Sharpe | Max drawdown |
| --- | ---: | ---: | ---: |
| Exact 2024-02–2026-06, no overlay | 41.50% | 1.410 | -26.23% |
| Exact budgeted call spread | 38.93% | 1.299 | -28.23% |
| 2007–2026 2× proxy, no overlay | 9.22% | 0.530 | -50.30% |
| 2007–2026 synthetic call spread | 8.14% | 0.470 | -50.30% |

One currently quoted SPY spread fit the $10,000 account at roughly $399 maximum
modeled loss including friction, but historical one-contract sizing was often
too large. Only 6 exact spreads cleared the limits; 2 won, and their combined
option P&L was -$606. Always buying the spread increased 2024 CAGR but worsened
full-period drawdown to -32.86% and reduced Sharpe. Smaller synthetic budgets
did not uncover an edge: a 2% annual budget still lowered CAGR from 9.22% to
8.97% and Sharpe from 0.530 to 0.516.

The synthetic model priced the completed exact entries at a median 1.55 times
their observed trade-bar debits, but a cheaper flat-VIX sensitivity still
failed. Lower-notional ETF alternatives lacked sufficiently complete listed
chains in the feasibility snapshot. No bullish options feature was added.

## Research record

Current studies write one JSON to `reports/` carrying a `decision` field, and
campaigns are summarized in `reports/strategy_campaign_YYYY-MM-DD.md`. Some
legacy reports predate the decision-field convention and should not be treated
as promotion records without their campaign context.

| Campaign | Outcome |
| --- | --- |
| [2026-07-23](../reports/strategy_campaign_2026-07-23.md) | No candidate passed; the best next investment is point-in-time data, not more parameter sweeps. |
| [2026-08-03](../reports/strategy_campaign_2026-08-03.md) | Removed the never-backtested stop and re-entry block from MOM_LS; rejected a correlation cap. |
| [2026-08-04](../reports/strategy_campaign_2026-08-04.md) | Fixed the shared panel/promotion-gate machinery and a live SPY position-cap bug; re-audited eight prior rejections on the corrected data — all confirmed, several relabeled or resolved on cleaner evidence. Built point-in-time fundamentals/insider data; quality filter deferred on coverage, accruals rejected under the standard gate but passes a risk-reduction gate in both held-out cells. |
| [2026-08-12](../reports/strategy_campaign_2026-08-12.md) | Rejected concentrated whole-share momentum and four fixed intraday families; validated timestamped news feasibility; stood down the 1DTE translator; started read-only 0DTE surface and execution-timing observation. |

### Execution-timing observation

The base-versus-2× execution-timing control can be inspected at any time with
`python -m scripts.execution_timing`. It read-only matches same-session,
same-symbol, same-side MOM_LS fills, weights slippage by matched reference
notional, and reports progress toward the frozen 100-pair control minimum. It
never changes cron automatically; reaching the threshold only authorizes a
separately reviewed 20-session candidate schedule.

### News-conditioned event study feasibility

A point-in-time Alpaca/Benzinga news audit found 147,305 articles from
2026-01-01 through 2026-07-31 with 100% usable first-publication timestamps.
A deterministic, preview-excluding label identified 11,102 unique
single-symbol earnings-result events across 3,766 symbols, comfortably passing
the pre-registered feasibility bar for a news-conditioned event study. This
does **not** validate true PEAD: the feed has no versioned pre-announcement
consensus estimates, so it cannot calculate standardized earnings surprise.
Raw vendor news is cached locally and gitignored.

### Intraday research

Intraday research shares one five-minute SPY/QQQ/IWM panel and execution
contract in `backtest/intraday.py`: completed-bar signals, next-bar-open entry,
same-session exit, 2 bp per leg primary ETF cost, and 5 bp per leg stress. The
2024-02 through 2026-07 Alpaca panel has 625 sessions and roughly 48,400 bars
per ETF. It is an IEX screening panel, not consolidated SIP data, and it does
not contain the 2020 or 2022 stress regimes.

Four fixed intraday families were screened without a parameter grid. Opening-
range continuation, VWAP mean reversion, and gap continuation lost after 5 bp
per leg in every ETF/window cell. Compression breakout was positive only in
IWM, with 18 and 24 trades — below the 30-trade minimum — and lost on SPY/QQQ.
No family qualified for option translation; changing thresholds after this
result would be tuning, not independent evidence. IWM compression breakout is
now under a pre-registered sequential (SPRT) forward test instead of a
fixed-N gate — see `reports/iwm_compression_breakout_forward_test_registration.json`
and `scripts/iwm_breakout_forward.py`.

The corresponding 1DTE defined-risk spread translator exists but is research-
interlocked. `python -m scripts.intraday_options_shadow` reads the committed
intraday decision before credentials; because no family qualified, it exits
without contacting Alpaca. It cannot submit orders. A future qualified signal
could authorize quote collection, not automatic paper activation.

The 2× morning cycle also runs a read-only SPY 0DTE surface collector. It
records the ATM straddle ask and a defined-risk OTM iron condor priced at short
bids and long asks, requiring positive displayed size on every leg. Missing or
after-hours markets produce no observation rather than a synthetic quote.
Directional 0DTE is disabled because no directional family qualified. Condor
"qualification" means only that a quote fits the declared credit/loss bounds;
at least 60 sessions of outcomes are required before designing paper orders.

Those decisions are binding. Check for an existing `decision` before proposing
a change — several plausible ideas are already tested and rejected, with
reasons. `AGENTS.md` covers the conventions and the traps in full, including
why several externally-sourced strategies keep failing here (selection bias,
beta mistaken for alpha, constraint mismatch, and gate strictness on noisy
statistics).
