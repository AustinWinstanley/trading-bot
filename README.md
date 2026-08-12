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
intentional residual position. MOM_LS alone runs without per-position stops or
a loss re-entry cooldown — see [Sleeves without stops](#sleeves-without-stops).

## Current research estimate

The production-equivalent backtest includes transaction costs, 3% short
borrow, and 5% margin financing, over 2020-07-28 through 2026-07-22:

| Portfolio | CAGR | Sharpe | Max drawdown |
| --- | ---: | ---: | ---: |
| Base SPY-core profile | 13.46% | 1.129 | -13.83% |
| 2× SPY-core profile | 20.77% | 0.918 | -26.82% |
| SPY buy and hold | 16.64% | 1.003 | -24.50% |

These are estimates, not forecasts. The individual-stock universe contains
currently listed companies and is survivorship-biased. Positive MOM_LS results
are therefore an optimistic upper bound. The sample is also short and dominated
by the post-2020 market.

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

**These numbers only describe what is deployed if production does not add
untested behaviour on top.** `backtest/production_portfolio.py` models MOM_LS as
a pure weekly rebalance — no stop-loss, no re-entry block. Production ran both
from launch until 2026-08-03, so the deployed strategy was measurably not the
one estimated here. `backtest/risk_overlay_study.py` quantified the gap and the
controls were removed from that sleeve. Before adding any risk control to a
sleeve, check whether the backtest models it; if not, the table above stops
describing reality.

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

A whole-share capacity study also modeled the actual $10,000 account sizes.
The base account realizes only 9.14% average short exposure versus its 15%
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
variant from 27.3% to 5.98% CAGR. Because nothing live reads 13F data any
more, the Sunday job no longer refreshes the holdings or CUSIP map unless
`paper_portfolio.sleeves.clone` is greater than zero — it was rebuilding the
map from ~36 SEC downloads weekly, leaving `state/thirteenf/cusip_map.parquet`
permanently dirty and able to raise a `CRITICAL` about a sleeve that is not
trading. `backtest/clone_study.py` and `backtest/production_portfolio.py` read
the committed snapshot; pass `refresh=True` if a study needs current data.

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

### Research record

Every study writes one JSON to `reports/` carrying a `decision` field, and
campaigns are summarized in `reports/strategy_campaign_YYYY-MM-DD.md`:

| Campaign | Outcome |
| --- | --- |
| [2026-07-23](reports/strategy_campaign_2026-07-23.md) | No candidate passed; the best next investment is point-in-time data, not more parameter sweeps. |
| [2026-08-03](reports/strategy_campaign_2026-08-03.md) | Removed the never-backtested stop and re-entry block from MOM_LS; rejected a correlation cap. |
| [2026-08-04](reports/strategy_campaign_2026-08-04.md) | Fixed the shared panel/promotion-gate machinery and a live SPY position-cap bug; re-audited eight prior rejections on the corrected data — all confirmed, several relabeled or resolved on cleaner evidence. Built point-in-time fundamentals/insider data; quality filter deferred on coverage, accruals rejected under the standard gate but passes a risk-reduction gate in both held-out cells. |

The base-versus-2× execution-timing control can be inspected at any time with
`python -m scripts.execution_timing`. It read-only matches same-session,
same-symbol, same-side MOM_LS fills, weights slippage by matched reference
notional, and reports progress toward the frozen 100-pair control minimum. It
never changes cron automatically; reaching the threshold only authorizes a
separately reviewed 20-session candidate schedule.

A point-in-time Alpaca/Benzinga news audit found 147,305 articles from
2026-01-01 through 2026-07-31 with 100% usable first-publication timestamps.
A deterministic, preview-excluding label identified 11,102 unique
single-symbol earnings-result events across 3,766 symbols, comfortably passing
the pre-registered feasibility bar for a news-conditioned event study. This
does **not** validate true PEAD: the feed has no versioned pre-announcement
consensus estimates, so it cannot calculate standardized earnings surprise.
Raw vendor news is cached locally and gitignored.

Those decisions are binding. Check for an existing `decision` before proposing
a change — several plausible ideas are already tested and rejected, with
reasons. `AGENTS.md` covers the conventions and the traps.

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

The server wrapper expects the checkout at `/home/austin/trading-bot`:

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
scripts/paper.sh daily 09:47      # second argument is the intended ET slot
scripts/paper.sh daily2x 09:51
scripts/paper.sh weekly           # omit the slot to run unconditionally
scripts/paper.sh health
scripts/paper.sh health2x
```

### The ET slot guard

The server clock is UTC and Debian cron has no `CRON_TZ`, so every ET slot
needs **two** crontab lines — one correct under EDT (UTC−4), one under EST
(UTC−5). Both fire year round. The optional second argument is the ET time the
job is meant to run at; a firing more than 5 minutes away from it exits as a
no-op, so exactly one of each pair does work whatever the offset.

Before this guard both lines of every pair ran, so each job executed **four**
times a weekday, and the out-of-season copy did not merely duplicate the run —
it traded an hour late. **Do not delete either line of a pair**, and give any
new market-hours job a slot argument. Jobs with no market-clock sensitivity
(the Sunday refresh) stay single-line and unguarded, because a slot guard would
skip them outright for half the year.

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

From the runs of 2026-08-04, daily reports **append** one `## run <timestamp>`
block per run under a single `# Paper <date>` heading. They previously
truncated, so the file on disk was always the last run of the day — reliably
the quiet one, every order having been placed hours earlier. Eight sessions of
real trading were journalled as `approved 0 | submitted 0`. Files dated
2026-08-03 or earlier therefore show one run out of several. When judging
whether the bot traded, trust `state/paper.db` over any report.

The 2× profile records a shadow 12% volatility-target recommendation from its
own daily paper-equity history. It requires 32 observations in a 63-session
window and may recommend 0.5×–2× exposure. `shadow` mode cannot alter target
weights or orders, and both checked-in profiles keep active scaling disabled by
default (`off` for base and `shadow` for 2×).

The implemented opt-in `active` mode applies the recommendation uniformly to
every target and its sleeve attribution before proposals reach the normal risk
gate. It may only reduce the fixed profile or restore it toward its configured
ceiling; it cannot exceed the original targets. Until enough observations
exist it retains fixed exposure. Historical returns earned under earlier active
scales are normalized before estimating fixed-profile volatility, preventing a
de-risk/re-leverage feedback loop. Existing rebalance bands, exposure caps,
stops, circuit breakers, and marketable-limit rules remain in force.

To begin an explicit 2× paper experiment, promote only
`paper_portfolio.volatility_overlay.mode` in `config_2x.yaml` from `shadow` to
`active` in a reviewed commit, then use the normal server upgrade and inspect
its dry-run's `recommended` and `applied` leverage before cron resumes. Do not
leave a server-only config edit that would dirty the deployment checkout.
Returning the value to `shadow` in another committed change immediately stops
new overlay scaling; ordinary rebalancing restores the fixed targets subject
to the same risk controls.

## Safety contract

`engine/risk.py` may reject or shrink a proposal. It may never enlarge one or
invent a symbol. Config validation refuses averaging down, non-limit entries,
shorting without an explicit cap, and inconsistent exposure limits.

Risk controls include:

- single-name, long, short, gross, and leveraged-ETF exposure caps;
- daily, monthly, and peak-drawdown circuit breakers;
- broker-held OTO stops for whole-share entries, with simple DAY fractional
  entries protected by the local monitoring fallback — *except* sleeves listed
  in `risk.stop_exempt_sleeves`, which carry no stop at all (see below);
- no averaging down;
- loss re-entry cooldown, except for `risk.reentry_block_exempt_sleeves`;
- liquidity, price, IPO-age, borrowability, and slippage filters;
- opening/closing entry windows.

### Sleeves without stops

`MOM_LS` is in both exemption lists, so its positions carry **no stop-loss and
no re-entry cooldown**. This is deliberate and evidence-backed
(`reports/risk_overlay_study.json`): the sleeve rebalances weekly, and stopping
it out cost 0.69–1.44pp of CAGR and Sharpe in both windows and both profiles
while raising turnover 36%. The re-entry block did nothing on its own — only a
stop-out leaves a name still ranked and therefore barred from a signal that
still likes it.

The gate invariant is unchanged in strength: an exempt sleeve must carry
*exactly* zero stop. A partial or malformed stop still fails the run loudly
rather than reaching the broker. `submit_entry` sends a plain limit when there
is no stop, because a zero stop inside an OTO is inert for a long and triggers
instantly for a short. The health check learns which positions are unstopped by
design from the journal's most recent opening order per symbol, so it does not
alarm on them — but it still flags every *other* unprotected position.

Emptying both lists restores stops and cooldowns everywhere.

This is experimental software, not investment advice.
