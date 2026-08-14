# trading-bot

A deterministic, rule-based paper-trading system for Alpaca. It runs a
diversified long-flat/long-short equity strategy — trend-following,
cross-sectional momentum, and a market-tracking core — through a
hand-written risk gate that can only ever reject or shrink an order, never
enlarge one or invent a symbol. Every backtest ships with realistic
transaction costs, and every strategy currently trading has cleared a
pre-registered promotion gate against out-of-sample data before going live.
No LLM has broker credentials or participates in sizing or execution.

> [!IMPORTANT]
> **This is experimental software, not investment advice.** It trades two
> Alpaca **paper** (simulated-money) accounts. Nothing in this repository is
> configured or intended for real-money trading. Backtest results describe
> a short, mostly post-2020 historical sample with realistic but still
> optimistic assumptions (see [docs/research.md](docs/research.md)) — they
> are not forecasts of future returns.

## What it does

- **Builds target portfolio weights** from completed daily market data —
  a 40% SPY core, a 15-asset trend-following overlay (TSMOM), a 200-day SPY
  trend filter, and a weekly cross-sectional long/short momentum sleeve
  (MOM_LS). A parallel 2× profile scales the same strategy for a second,
  fully separate paper account used as a leverage/experimentation lab.
- **Diffs targets against live broker state** — Alpaca is the source of
  truth for positions, orders, and fills; nothing is assumed from local
  state alone.
- **Passes every proposal through a risk gate** (`engine/risk.py`) that can
  reject or shrink an order but is runtime-asserted to never enlarge one or
  invent a symbol — position/exposure/leverage caps, circuit breakers,
  broker-held stops, no-averaging-down, liquidity/borrowability/IPO-age
  filters, and more.
- **Submits orders and reconciles the journal** back to actual broker state
  after every run.
- **Runs a capped, pre-registered "lab" tier** on the 2× account for ideas
  that haven't cleared the full promotion bar yet — read-only shadow
  observation or tightly capped real paper orders, never silently promoted.
- **Ships a read-only dashboard and MCP debug server** for live visibility
  into both accounts, architecturally incapable of placing an order (see
  [Architecture](docs/architecture.md)).

See [docs/architecture.md](docs/architecture.md) for the full request path,
the trust model between the trading engine and the read-only services, and
why several cron locks are deliberately shared between jobs.

## Deployed portfolio

The base profile targets at most 100% long, 15% short, and 115% gross
exposure:

| Sleeve | Exposure | Construction |
| --- | ---: | --- |
| Equity core | 40% long | SPY |
| TSMOM | 25% long/flat | 15 asset ETFs, 12-month trend, inverse volatility |
| Trend | 20% long/flat | SPY above its 200-day average |
| MOM_LS | 15% long + 15% short | Weekly 12-1 momentum, top/bottom 20 |

The 2× profile scales targets to at most 200% long, 30% short, and 230%
gross, using entirely separate credentials, state, journal, and reports.

MOM_LS stands down when its weekly target file is absent or stale. Cash is
an intentional residual position. MOM_LS alone runs without per-position
stops or a loss re-entry cooldown — deliberate and evidence-backed, see
[Sleeves without stops](docs/paper-attribution.md#sleeves-without-stops).

### Strategy status

| Status | Components | Effect on paper orders |
| --- | --- | --- |
| Active | Equity core, TSMOM, trend, MOM_LS | Build targets that pass through the normal risk gate and may submit paper orders. |
| Shadow/read-only | 2× volatility recommendation; options, momentum-options, event-volatility, and 0DTE collectors | Record recommendations or displayed quotes only; cannot submit orders. |
| Standing down | 1DTE intraday spread translator | Exits before contacting Alpaca because no directional intraday family qualified. |
| Rejected/deferred | Strategies whose committed report decision did not clear its pre-registered gate | Not included in portfolio targets; reconsideration requires new evidence. |

Nothing moves from research or shadow to active merely because its collector
runs under cron. Activation requires a reviewed config/code change and the
normal upgrade verification.

## Research

The headline backtest — transaction costs, 3% short borrow, and 5% margin
financing included, 2020-07-28 through 2026-07-22 — puts the base profile at
13.46% CAGR / 1.129 Sharpe / -13.83% max drawdown against SPY buy-and-hold's
16.64% / 1.003 / -24.50%. **This is an idealized-capacity estimate, not a
forecast** — see [docs/research.md](docs/research.md) for the full picture,
including whole-share short-capacity constraints, delisting sensitivity,
bootstrap confidence ranges, a 2007-2026 long-history stress proxy, and every
rejected candidate with its reasoning.

| Campaign | Outcome |
| --- | --- |
| [2026-07-23](reports/strategy_campaign_2026-07-23.md) | No candidate passed; the best next investment is point-in-time data, not more parameter sweeps. |
| [2026-08-03](reports/strategy_campaign_2026-08-03.md) | Removed the never-backtested stop and re-entry block from MOM_LS; rejected a correlation cap. |
| [2026-08-04](reports/strategy_campaign_2026-08-04.md) | Fixed the shared panel/promotion-gate machinery and a live SPY position-cap bug; re-audited eight prior rejections on the corrected data. |
| [2026-08-12](reports/strategy_campaign_2026-08-12.md) | Rejected concentrated whole-share momentum and four fixed intraday families; validated timestamped news feasibility; started read-only 0DTE and execution-timing observation. |

Every study writes one JSON to `reports/` with a pre-registered `decision`
field. Existing decisions are binding — reopening one needs new evidence,
not a fresh opinion.

## Getting started

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env   # add your own Alpaca paper + data-provider keys
.venv/bin/python -m pytest
.venv/bin/python -m scripts.run_daily --dry-run --force
```

See [docs/operations.md](docs/operations.md) for full setup, verification,
and deployment instructions, and [CONTRIBUTING.md](CONTRIBUTING.md) before
opening a PR.

## Safety contract

`engine/risk.py` may reject or shrink a proposal. It may never enlarge one
or invent a symbol — enforced by a runtime assertion, not just review. Risk
controls include single-name/long/short/gross/leveraged-ETF exposure caps;
daily/monthly/peak-drawdown circuit breakers; broker-held stops (with one
evidence-backed, explicitly listed exemption — see
[docs/paper-attribution.md](docs/paper-attribution.md)); no averaging down;
loss re-entry cooldowns; liquidity/price/IPO-age/borrowability/slippage
filters; and opening/closing entry windows. See
[`SECURITY.md`](SECURITY.md) for the full trust model across the trading
engine and the read-only dashboard/MCP services.

## Documentation

- [docs/architecture.md](docs/architecture.md) — request path, trust tiers, cron job/lock table
- [docs/research.md](docs/research.md) — full backtest evidence and rejected candidates
- [docs/operations.md](docs/operations.md) — setup, verification, deployment
- [docs/paper-attribution.md](docs/paper-attribution.md) — journal schema, volatility overlay, options experiment
- [docs/data.md](docs/data.md) — rebuilding vendor data caches
- [`AGENTS.md`](AGENTS.md) — the full operating contract for anyone (human or AI agent) changing this code
- [`SECURITY.md`](SECURITY.md) · [`CONTRIBUTING.md`](CONTRIBUTING.md) · [`LICENSE`](LICENSE) (MIT)

This is experimental software, not investment advice.
