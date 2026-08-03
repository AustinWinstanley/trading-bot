# Strategy research campaign — 2026-08-03

## Decision

Remove the per-position stop-loss and the loss re-entry block from MOM_LS.
Both were running in production and neither had ever been backtested on that
sleeve. Keep the sleeve's bottom-20 construction unchanged: a correlation cap
was tested and rejected.

The campaign began as an operational review of eight sessions of paper reports
that read `approved 0 | submitted 0`. That reading was an artifact — the daily
job ran four times a weekday and each run overwrote the report — but tracing it
surfaced a larger problem. The deployed strategy was not the strategy the
research had validated.

## Results

| Candidate | Decision | Most important result |
|---|---|---|
| Remove stop-loss from MOM_LS | **Adopt** | Cost 0.69–1.44pp of CAGR *and* Sharpe in both windows and both profiles; turnover +36%. |
| Remove loss re-entry block from MOM_LS | **Adopt** | Inert without the stop (0 blocked selections). With it, mixed and window-dependent: −0.28/−0.59pp early, +0.34/+0.73pp held-out. |
| Correlation cap on MOM_LS selection | **Reject** | Diversified as designed (mean pairwise correlation 0.313 → 0.202, basket vol 43.1% → 32.1%) and lost CAGR and Sharpe in all 8 cells at every threshold. |
| Universe dollar-volume floor $20M → $3M | **Adopt** | Same 29 proposals went from 0 approved to 16. No research covers this gate; it was sized for institutional capital. |

## New information worth retaining

### The backtest did not model the deployed risk controls

`backtest/production_portfolio.py` models MOM_LS as a pure weekly 12-1
rebalance. It contains no stop-loss and no re-entry block. `config.yaml`
applied both to every sleeve. The reported Sharpe of 1.04 therefore described
a strategy that was not running.

This is the most reusable lesson here, and it generalizes past this sleeve:
**a risk control added in config is a strategy change, and inherits no evidence
from a backtest that does not model it.** Before adding one, check whether the
relevant study simulates it.

`backtest/risk_overlay_study.py` closes the gap. Its control reproduces
`build_portfolio` to 9e-8, so its variants measure the overlay rather than a
second implementation of the sleeve.

### The re-entry block is an amplifier, not a control

Without stops it never fires. A rotation exit drops a name from the ranks, so
nothing is barred from re-entry. Only a stop-out leaves a name *still ranked*
and then blocked from a signal that still likes it.

Observed live: ten longs stopped out 07-27/28 at −16% to −23%, barred five
days, re-entered 08-03 at a mean **+3.5%** above the exit price (WDC +18.6%,
BE +13.8%, STX +12.7%). Sold the drawdown, sat out the rebound, bought back
higher.

Note the block fires on **any** exit at an unrealized loss, not just stop-outs
(`scripts/run_daily.py`, "revenge-trade block"). Modelling only stop exits made
it look free; correcting that is what revealed the amplifier mechanism.

### Momentum concentration is a feature, not a defect

The 2026-07-23 loss came from ten longs that were one trade — AI datacenter
hardware. Realized mean pairwise correlation 0.56, basket volatility 83%
annualized against SPY's 13.5%, all ten down 14–32% in a week SPY rose 0.4%.
They produced 95% of realized losses.

But the historical long book averages **0.313** mean pairwise correlation
(median 0.296, p90 0.466, max 0.646). The live week was a top-decile draw, not
a structural break. And capping correlation removes the signal: momentum *is* a
factor bet, and de-correlating it costs more than the concentration does.

This is consistent with, and strengthens, the existing defer in
`reports/sector_neutral_momentum_feasibility.json`. Correlation needs no dated
industry map and carries no classification look-ahead, so it tests the same
hypothesis with data already in hand — and it fails. That reduces the expected
value of acquiring the SIC panel for this purpose specifically.

### Gates must be sized to the account

The $20M dollar-volume floor rejected ~20 names a day. At a $75 slot that is
0.004% of a $2M-ADV name's daily volume. Two other gates are still binding for
account-size reasons and are worth revisiting together:

- whole-share shorts: a $75 slot cannot short anything priced above $75, which
  biases the "market-neutral" sleeve net long (see
  `reports/short_capacity_study.json`, which measured 60.9% realized short
  capacity and deferred a price-aware fix);
- `tsmom_min_dollar_volume` still rejects FXE at $243k ADV.

## Limitations

Unchanged from the rest of this sleeve's research: historical easy-to-borrow
and locate availability are unavailable, and the cross-sectional universe is
survivorship-biased, so positive MOM_LS results remain optimistic bounds.

Specific to the overlay study: ATR14 cannot be computed from the close/volume
cache, so the ATR variant uses an inflated close-to-close proxy bracketed by
flat 8% and 15% variants, which agree. Stops are evaluated on closes, while
production places real broker stops on whole-share shorts that can trigger
intraday — so the measured cost of the stop is, if anything, understated.

## Operational findings from the same review

These are not strategy results but they changed what the reports mean.

- **The daily job ran four times a weekday per profile.** Each ET slot needs
  two UTC crontab lines because the server is UTC and Debian cron has no
  `CRON_TZ`; both fired year round, and the out-of-season copy traded an hour
  late rather than merely duplicating. `scripts/paper.sh` now takes the
  intended ET slot and no-ops when the clock disagrees.
- **Daily reports truncated instead of appending**, so the committed file was
  always the last and quietest run of the day. Eight sessions of real trading
  were journalled as `approved 0 | submitted 0`. Trust `state/paper.db` over
  any single report block.
- **The weekly `CRITICAL` scan was windowed by file count, not date**, so it
  kept resurfacing 07-23 incidents as if current.
- **The Sunday job refreshed 13F/CUSIP data for a retired sleeve**, leaving a
  parquet permanently dirty and able to raise a `CRITICAL` about a strategy
  that is not trading.
