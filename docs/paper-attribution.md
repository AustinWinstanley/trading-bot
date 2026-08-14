# Paper attribution

How the paper journals record trades, exposure, and the experimental
volatility overlay — the detail behind what the dashboard and weekly reports
summarize.

## Journal schema and reports

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

## Volatility overlay (2× shadow)

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

## Sleeves without stops

`MOM_LS` is in both risk-gate exemption lists, so its positions carry **no
stop-loss and no re-entry cooldown**. This is deliberate and evidence-backed
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

Emptying both exemption lists in config restores stops and cooldowns
everywhere.

## Options experiment (2× only)

Separate from the equity portfolio above, the 2× lab also runs a capped,
pre-registered options experiment (`bull_put_delta_selected_live`) that
submits real (paper-broker) multi-leg orders through its own parallel risk
gate. See `AGENTS.md`'s "Real options paper trading" section for the full
design — defined-risk-only, one structure at a time, a hard per-experiment
loss cap that automatically stands the experiment down, and a `reports/experiments/`
registration document disclosing exactly how much evidence it had at
promotion time.
