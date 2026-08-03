# Strategy research campaign — 2026-07-23

## Decision

Do not change the paper-trading strategy mix from this campaign. None of the
five executable candidates passed its frozen early/held-out promotion rule.
Three research ideas remain deferred until point-in-time data can support an
honest test; they are not rejected and should not be approximated with
look-ahead-prone substitutes.

The best next investment is data, not another parameter sweep:

1. dated SEC SIC/industry classifications for sector-neutral momentum;
2. reproducible point-in-time SEC accounting panels for quality filtering;
3. timestamped earnings announcements and pre-announcement consensus snapshots
   for true PEAD.

## Results

| Candidate | Decision | Most important result |
|---|---|---|
| Momentum 20/40 rank buffer | Reject | Turnover fell about 49%–50%, but net Sharpe declined in both windows and both profiles. |
| Panic-rebound short gate | Reject | Marginally helped base, but reduced 2x CAGR/Sharpe; it never activated after 2023. |
| Sector-neutral momentum | Defer | No dated industry map exists; present-day labels would add classification look-ahead. |
| Defensive underlying mandate for 2x | Reject | Household drawdown improved, but early CAGR fell 8.73%→6.18% and Sharpe 0.545→0.449. |
| Invest idle trend reserve in SHY | Reject | Helped after 2023, but 2022 duration losses made the early window worse; full-period incremental return was about −0.02% annualized. |
| Point-in-time quality filter | Defer | Prior quality signal changed sign between halves and its source SEC parquet is absent. |
| SPY/IVV and GLD/IAU pairs | Reject | Helped early, but held-out Sharpe fell 1.336→1.319 and drawdown worsened 13.99%→14.38%. |
| True PEAD | Defer | Announcement timestamps and pre-announcement estimates are absent. Generic upside-gap drift was negative and is rejected as a proxy. |

## Current estimated baseline

Using the same 2020-02-13 through 2026-07-22 research window and the current
whole-share short-capacity model:

| Profile | CAGR | Sharpe | Max drawdown |
|---|---:|---:|---:|
| Base | 12.33% | 1.041 | −13.99% |
| 2x | 18.95% | 0.868 | −26.23% |

These are model estimates, not forecasts. The cross-sectional universe is
primarily current listings, historical easy-to-borrow data is unavailable,
and the test window is short. Positive estimates remain survivorship-biased
upper bounds.

## New information worth retaining

The momentum leg changed character across regimes. From 2020–2022, its short
leg contributed while its long leg was weak. From 2023 onward, the long leg
became strongly positive and the short leg lost money. This is economically
important, but the fixed panic gate did not predict that change and therefore
does not justify discretionary or automatic short timing.

Small-account mechanics remain binding. Whole-share shorts materially reduce
gross exposure and also damaged the pairs overlay. Future short strategies
should be evaluated at the intended account size before signal research is
treated as implementable.

## Research discipline

The rank-buffer design followed the trading-cost mitigation prior that
buy/hold spreads can reduce turnover, but the saved costs did not offset signal
dilution in this portfolio. The result is consistent with the caution in
[A Taxonomy of Anomalies and Their Trading Costs](https://www.nber.org/papers/w20721).

The panic gate was motivated by the documented concentration of momentum
crashes in volatile rebounds following market declines in
[Momentum Crashes](https://www.nber.org/papers/w20439), but this particular
tradable rule did not generalize across account profiles.

Sector-neutral momentum remains plausible given evidence for
[industry-relative characteristics](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=213872),
but it requires dated classifications. Likewise, historical pairs profits in
[Pairs Trading](https://www.nber.org/papers/w7032) did not survive this
small-account, cost-aware held-out test. PEAD remains a data project; the
literature also documents that drift has weakened as information dissemination
improved in
[Information Diffusion and Post-Earnings-Announcement Drift](https://academic.oup.com/rfs/article-abstract/28/4/1242/1928671).

## Implementation decision

No runtime or checked configuration changes are warranted. Continue the
existing base and 2x paper accounts and the already configured shadow research
features. Revisit the three deferred branches only after their minimum data
contracts in the corresponding reports are satisfied.
