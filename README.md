# trading-bot

Deterministic Alpaca paper-trading system with a pure-Python risk gate,
broker-state reconciliation, and reproducible backtests. No LLM has broker
credentials or participates in sizing or execution.

**Status:** two active $10,000 paper profiles are configured: an unlevered
account and a parallel 2× experiment. Nothing in this repository
enables live trading.

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

## Deployed portfolio

The base profile has at most 100% long, 15% short, and 115% gross exposure:

| Sleeve | Exposure | Construction |
| --- | ---: | --- |
| Equity core | 40% long | SPY |
| TSMOM | 25% long/flat | 15 asset ETFs, 12-month trend, inverse volatility |
| Trend | 20% long/flat | SPY above its 200-day average |
| MOM_LS | 15% long + 15% short | Weekly 12-1 momentum, top/bottom 20 |

The 2× profile scales targets to at most 200% long, 30% short, and 230% gross.
It uses separate credentials, state, journal, and reports.

MOM_LS stands down when its weekly target file is absent or stale. Cash is an
intentional residual position.

## Current research estimate

The production-equivalent 2020–2026 backtest includes transaction costs, 3%
short borrow, and 5% margin financing:

| Portfolio | CAGR | Sharpe | Max drawdown |
| --- | ---: | ---: | ---: |
| Base SPY-core profile | 12.55% | 1.081 | -13.23% |
| 2× SPY-core profile | 19.13% | 0.879 | -25.81% |
| SPY buy and hold | 14.99% | 0.883 | -24.50% |

These are estimates, not forecasts. The individual-stock universe contains
currently listed companies and is survivorship-biased. Positive MOM_LS results
are therefore an optimistic upper bound. The sample is also short and dominated
by the post-2020 market.

A Tiingo-metadata/Alpaca-bars sensitivity test added 1,481 delisted listings.
The observed-last-price case and a deliberately severe case that assigns every
delisting a total loss bound the production estimate:

| Delisting assumption | Base CAGR | Base Sharpe | 2× CAGR | 2× Sharpe |
| --- | ---: | ---: | ---: | ---: |
| Observed last price | 12.45% | 1.072 | 18.92% | 0.871 |
| Every delisting loses 100% | 11.66% | 1.011 | 17.26% | 0.810 |

MOM_LS remains positive across those bounds, but historical borrowability and
actual delisting proceeds are unavailable, so this reduces rather than removes
the uncertainty.

A whole-share capacity study also modeled the actual $10,000 account sizes.
The base account realizes only 9.14% average short exposure versus its 15%
target because 25.0% of ranked shorts round below one share. The 2× account
realizes 25.04% versus 30%, with 3.9% rounding to zero:

| Construction | Base short | Base CAGR / Sharpe | 2× short | 2× CAGR / Sharpe |
| --- | ---: | ---: | ---: | ---: |
| Fractional benchmark | 15.00% | 12.63% / 1.087 | 30.00% | 19.31% / 0.886 |
| Current whole-share bottom 20 | 9.14% | 12.33% / 1.041 | 25.04% | 18.95% / 0.868 |
| Price-aware bottom 20 | 12.12% | 12.30% / 1.055 | 26.06% | 18.92% / 0.868 |
| Whole-share bottom 10 | 12.26% | 12.57% / 1.061 | 27.62% | 18.64% / 0.849 |

Neither alternative dominates across the early and held-out windows, so the
current construction remains deployed while real paper fills accumulate.
Historical easy-to-borrow status is unavailable, making all variants optimistic
execution bounds.

A 5,000-path, 63-session block bootstrap puts useful ranges around those point
estimates. With the deliberately severe universal-zero delisting drag applied:

| Profile / horizon | CAGR p05 | Median | CAGR p95 | Chance of loss | Drawdown p05 |
| --- | ---: | ---: | ---: | ---: | ---: |
| Base, 1 year | -4.3% | 12.4% | 31.3% | 11.4% | -15.8% |
| Base, 3 years annualized | 2.2% | 12.3% | 22.9% | 2.0% | -20.3% |
| 2×, 1 year | -13.1% | 18.4% | 60.4% | 18.1% | -30.4% |
| 2×, 3 years annualized | -1.2% | 18.4% | 41.1% | 6.4% | -39.1% |

These are conditional scenario ranges, not calibrated forecast probabilities.
Resampling cannot create crises or regimes absent from the short 2020–2026
source history, so capital planning should allow outcomes worse than p05. A
21/63/126-session block sensitivity moved the three-year p05 CAGR to 1.0%–2.9%
for base and -3.5% to 0.2% for 2×; the 2× drawdown p05 reached -43.7%.

A separate 2007–2026 stress proxy reconstructs the SPY, trend, and 15-asset
TSMOM sleeves and substitutes the point-in-time
[Kenneth French daily momentum factor](https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/Data_Library/det_mom_factor_daily.html)
for unavailable pre-2020 stock ranks:

| Long-history proxy | CAGR | Sharpe | Max drawdown | GFC return | COVID-crash return |
| --- | ---: | ---: | ---: | ---: | ---: |
| Base | 7.80% | 0.785 | -26.25% | -15.60% | -16.50% |
| 2× | 9.22% | 0.530 | -50.30% | -34.11% | -31.25% |

The proxy’s 2020–2026 correlation to the actual capacity-adjusted MOM_LS
contribution is only 0.55–0.60. These are stress estimates, not a reconstructed
production backtest, but the limited incremental return and much larger
drawdown keep 2× strictly experimental.

The two $10,000 paper accounts should be judged as one $20,000 portfolio.
Because they trade the same sleeves, an equal-dollar split is approximately a
1.5× portfolio rather than a diversified pair:

| Capital in 2× account | Nominal combined leverage | Recent CAGR / max drawdown | 2007–2026 proxy CAGR / max drawdown |
| ---: | ---: | ---: | ---: |
| 0% | 1.00× | 12.33% / -13.99% | 7.80% / -26.25% |
| 25% | 1.25× | 14.10% / -17.18% | 8.26% / -33.05% |
| 50% (current paper split) | 1.50× | 15.80% / -20.28% | 8.66% / -39.31% |
| 75% | 1.75× | 17.42% / -23.30% | 8.98% / -45.05% |
| 100% | 2.00× | 18.95% / -26.23% | 9.22% / -50.30% |

For the current equal split, the severe recent-history bootstrap estimates a
15.5% chance of a negative one-year CAGR and a 46.1% chance of a drawdown over
20% during a three-year path. The long-history proxy lost 25.3% through the
GFC window and 24.1% during the COVID crash, with drawdowns of 30.3% and 24.4%
respectively. These estimates support keeping the equal split as an aggressive
paper experiment, while treating its combined risk budget and drawdown as
1.5× exposure.

A volatility de-risking overlay on that long-history 2× proxy updates weekly,
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
deployment.

The former 13F clone sleeve was removed after a timezone-mixed forward-fill
bug was found. Correct point-in-time results reduced its estimated conviction
variant from 27.3% to 5.98% CAGR.

A cost-aware overnight-equity study also rejected replacing the SPY core.
Close-to-open SPY broke even at only 1.91 bp per execution in the post-2013
sample. QQQ survived 2 bp standalone, but every tested SPY/QQQ overnight
replacement reduced production-portfolio Sharpe in the held-out window.

A defensive-rotation study also rejected investing the 20% trend sleeve while
its SPY signal is off. The pre-selected 12-month relative-momentum rule rotated
among GLD, TLT, IEF, and cash using prior-day data and paid 8 bp per unit of
turnover. In the 2007–2016 design window it reduced proxy CAGR from 4.76% to
4.12%, Sharpe from 0.527 to 0.453, and worsened drawdown from -26.25% to
-31.66%. Its apparent improvement in the recent held-out window therefore did
not qualify it for a paper shadow. Fixed gold looked attractive recently but
worsened long-history held-out drawdown, so it remains an exploratory
observation rather than a selected strategy.

A sleeve-specific volatility overlay produced a mixed result. The pre-selected
15% target scaled only MOM_LS, weekly, between 25% and 100% of its fixed
allocation. It improved the base long-history proxy from 7.80% to 8.52% CAGR
and reduced full-period drawdown from -26.24% to -21.43%, including turning the
2009 momentum-crash rebound proxy from -5.5% to +5.9%. But it worsened the GFC
return from -15.6% to -18.5%, did not improve 2017+ maximum drawdown, and
reduced exact 2020–2022 Sharpe from 0.615 to 0.589. It therefore failed its
pre-specified cross-window rule and is not promoted to paper shadow.

A $5,000 pilot / $10,000 2× follower simulation tested whether the larger
account should wait for individual pilot MOM_LS positions to turn
directionally positive. The pre-selected one-session rule retained only 35.4%
of follower momentum exposure, reduced full-sample CAGR from 18.95% to 13.49%,
and worsened drawdown from -26.23% to -32.14%. A five-session variant improved
2023+ results but failed badly in 2020–2022. Delaying every trade also failed,
showing that neither confirmation nor delay was robust. No paper feature flag
was added; the result does not rule out strategy-level allocation after a
pilot accumulates many independent experimental trades.

Options are not currently traded by the system. The original options study
rejected only passive cash-secured put writing and fully covered calls. A
broader Cboe benchmark screen subsequently tested protective puts, collars,
put-spread collars, iron condors, iron butterflies, partial covered calls, and
alternative put-write schedules as 20% replacements for the SPY core.

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

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
chmod 600 .env
```

Required environment variables:

- `ALPACA_API_KEY`, `ALPACA_API_SECRET`
- `ALPACA_API_KEY_2X`, `ALPACA_API_SECRET_2X`
- `TIINGO_API_TOKEN` for historical research

All configured Alpaca credentials must point to paper accounts.

## Verification

```bash
.venv/bin/python -m pytest
.venv/bin/python -m backtest.production_portfolio
.venv/bin/python -m backtest.allocation_study
.venv/bin/python -m backtest.capital_split_study
.venv/bin/python -m backtest.defensive_rotation_study
.venv/bin/python -m backtest.momentum_sleeve_overlay_study
.venv/bin/python -m backtest.pilot_follower_study
.venv/bin/python -m backtest.options_strategy_study
.venv/bin/python -m backtest.spy_put_spread_study
.venv/bin/python -m backtest.anticipatory_tail_hedge_study
.venv/bin/python -m backtest.bull_call_spread_study
.venv/bin/python -m scripts.run_daily --dry-run --force
.venv/bin/python -m scripts.run_daily --dry-run --force --profile 2x
```

`--dry-run` performs broker reads but submits no orders and rolls back all
local journal/report state.

To rebuild the survivorship-biased cross-sectional research matrix:

```bash
.venv/bin/python -m backtest.xsec_data \
  --build-universe \
  --universe state/universe_stocks.json \
  --start 2020-01-01
```

## Deployment

The server wrapper expects the checkout at `/home/user/trading-bot`:

```bash
scripts/upgrade.sh
```

`upgrade.sh` backs up the user crontab, pauses only `scripts/paper.sh` jobs,
waits for their locks, temporarily stashes tracked server-generated paper
reports, fast-forwards from `origin/main`, installs dependencies, runs tests,
executes both mutation-free dry runs, and checks both paper accounts. It runs
from a temporary copy so pulling cannot modify the executing script. It
restores the report stash on every exit and restores the exact original
crontab only on success. If any check fails, trading cron remains paused and
the script prints the one-line cron recovery command using its timestamped
backup in `state/`. During an upgrade only, a completely unused profile with
an active account, no positions, no orders, and an empty journal may pass
without a snapshot; the regular scheduled health check remains strict until
the profile completes its first live run.

Individual wrapper jobs remain available:

```bash
scripts/paper.sh daily
scripts/paper.sh daily2x
scripts/paper.sh weekly
scripts/paper.sh health
scripts/paper.sh health2x
```

The wrapper uses `flock` to prevent overlapping jobs and `timeout` to bound
stalled runs. Health jobs fail on stale snapshots, inactive accounts, stale
parent orders, or unprotected positions; schedule them separately so a missed
daily job is observable. Deploy only reviewed commits, run the test suite on
the server, then run both dry-run commands before enabling cron.

## Paper attribution

The paper journals automatically apply additive schema migrations and retain
the old rows. New live runs record requested versus gate-approved notional,
reference price, filled quantity and price, fill timestamp, and enriched
rejection details. Missing fill telemetry on older terminal orders is
backfilled from Alpaca when possible.

Each live snapshot also records target and actual long, short, and gross
exposure. Shared symbols are allocated proportionally across their originating
sleeves; holdings with no current target remain visible as `unattributed`.
Weekly reports show fill rate, adverse slippage, gate shrinkage, whole-share and
borrow rejections, latest sleeve exposure, and sleeve-level unrealized P&L for
both paper accounts. Dry runs wrap schema migrations in the same rolled-back
transaction as journal changes.

The 2× profile also records a shadow 12% volatility-target recommendation from
its own daily paper-equity history. It requires 32 observations in a 63-session
window and may recommend 0.5×–2× exposure, but `shadow` mode cannot alter target
weights or orders. The base profile is explicitly `off`; config validation
rejects any unimplemented active mode.

## Safety contract

`engine/risk.py` may reject or shrink a proposal. It may never enlarge one or
invent a symbol. Config validation refuses averaging down, non-limit entries,
shorting without an explicit cap, and inconsistent exposure limits.

Risk controls include:

- single-name, long, short, gross, and leveraged-ETF exposure caps;
- daily, monthly, and peak-drawdown circuit breakers;
- broker-held OTO stops for whole-share entries, with simple DAY fractional
  entries protected by the local monitoring fallback;
- no averaging down;
- loss re-entry cooldown;
- liquidity, price, IPO-age, borrowability, and slippage filters;
- opening/closing entry windows.

This is experimental software, not investment advice.
