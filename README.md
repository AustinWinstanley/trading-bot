# trading-bot

Scheduled-Claude trading bot. Deterministic Python owns the math and the risk;
Claude owns the judgment layer on top of a pre-computed candidate set.

**Status: Phase 0 — safety layer built, strategy sleeves not yet implemented.**
Not trading anything, paper or live.

## The one architectural rule

**The analyst LLM never holds broker credentials and never sizes a position.**

```
cron ─▶ engine/signals.py     deterministic: bars, indicators, regime, ranked candidates
        (no LLM)                                          ─▶ state/signals.json
                                                              │
cron ─▶ claude -p  ANALYST    reads signals + LESSONS.md, researches news/filings
        (agent/premarket.md)  for CONFIRMATION and VETO only. No broker tools.
                                                          ─▶ state/proposals.json
                                                              │
cron ─▶ engine/risk.py        THE GATE. Pure Python. May only reject or shrink,
        (no LLM)              never enlarge, never invent. Attaches stops.
                                                          ─▶ state/orders_approved.json
                                                              │
cron ─▶ EXECUTOR              Phase 2: Python → Alpaca REST.
                              Phase 3: a separate `claude -p` that replays the
                              approved file verbatim through the Robinhood MCP.
```

The analyst and the executor are separate processes with separate prompts and
the deterministic gate between them. This matters because Robinhood's MCP is
tool-based — an LLM must ultimately place the order — so that LLM's job is
reduced to mechanical replay of an already-validated file.

## The gate's contract

`engine/risk.py` has exactly two powers: **reject** a proposal or **shrink** it.
It can never enlarge one and never invent one. That invariant is asserted at
runtime in `_assert_gate_invariants()` and covered by `tests/test_risk_gate.py`.

Every limit lives in `config.yaml` — nothing is hardcoded. An invalid config
raises and halts trading rather than falling back to defaults. Several settings
are refused outright at load time regardless of what the file says:
`risk.allow_averaging_down: true`, `universe.allow_short: true`, and any
`execution.order_type` other than `limit`.

Position sizing is validated against **cash**, never `buying_power`. The Alpaca
paper account is margin-enabled (4x), and sizing off buying power would let the
bot quietly take 4x leverage.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install pandas numpy pyyaml requests pytest
cp .env.example .env      # then fill in credentials; chmod 600
```

## Tests

```bash
.venv/bin/python -m pytest
```

`tests/test_risk_gate.py` feeds the gate deliberately hostile proposals —
oversized positions, missing stops, averaging down, revenge trades, penny
stocks, illiquid names, trades during a kill switch, malformed JSON, NaN
notionals — and asserts every one is refused or cut to size. This suite gates
every change to the gate.

## Layout

| Path | What |
| --- | --- |
| `config.yaml` | Every risk limit. Version-controlled so each change is an auditable diff. |
| `engine/config.py` | Schema-validated loader. Refuses dangerous settings. |
| `engine/risk.py` | The gate. |
| `engine/signals.py` | *(Phase 1)* Momentum, RSI(2), PEAD scan, regime filter. |
| `engine/execute.py` | *(Phase 1)* Broker adapter: Alpaca paper, later Robinhood MCP. |
| `agent/*.md` | Prompts for each scheduled run. |
| `lessons/` | Evidence-gated long-term memory. |
| `state/` | Runtime handoff files + SQLite journal. Gitignored. |
| `exports/` | CSV exports of trades and equity — the durable, diffable backup. |

## Expectations

The target is 10%/month. Compounded that is ~214%/year, which is above any
sustained track record in existence. This system aims at outsized months through
a deliberately aggressive sleeve while the risk layer guarantees the account
survives the losing ones. Honest expectation: **~1.5–3%/month average, with
25–35% of months negative.**

Not investment advice.
