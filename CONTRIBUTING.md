# Contributing

Read this before opening a PR. This is a live paper-trading system — see
"What this is" below before assuming a change is purely cosmetic.

## What this is

This repository runs a real (paper-money) trading strategy on a schedule.
Two Alpaca **paper** accounts trade against it continuously; nothing here
executes with real money, but the code paths, risk gate, and research
conventions are the same ones that would matter if it did. Read
[`AGENTS.md`](AGENTS.md) before touching `engine/`, `scripts/`, or
`config*.yaml` — it documents the conventions that keep the risk gate
correct and the research honest, and most of what looks like unnecessary
caution there is load-bearing.

## Dev setup

```bash
python3.13 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env   # fill in your own API keys; see AGENTS.md/docs/data.md
```

The dashboard (`dashboard/`) and MCP debug server (`mcp_server/`) have
their own `requirements.txt` files, needed only if you're working on
those.

## Running tests

```bash
.venv/bin/python -m pytest -q
```

The suite is fully offline — no network calls, no credentials required. It
should pass on a fresh checkout with an empty `.env`. If you're touching
the risk gate, portfolio construction, or config loading, also run:

```bash
.venv/bin/python -m scripts.run_daily --dry-run --force
.venv/bin/python -m scripts.run_daily --dry-run --force --profile 2x
```

Dry runs are mutation-free (they roll back journal writes and never touch
`reports/`), so they're safe to run against a real `.env` with real paper
credentials.

## Research changes

A change to a strategy, risk control, or portfolio construction needs
backtest evidence, not just a passing test suite. `AGENTS.md`'s "Research
conventions" section describes the promotion gate
(`backtest.promotion.passes_gate`), the frozen validation window, and why
several plausible-sounding ideas were already tried and rejected — check
`reports/*.json` for an existing `decision` before proposing something
that looks similar. Overriding a prior decision needs new evidence, not a
fresh opinion.

## Commit messages

This repo uses [Conventional Commits](https://www.conventionalcommits.org/)
(`feat:`, `fix:`, `chore:`, `docs:`, `test:`, `refactor:`, ...) — see
AGENTS.md for the full convention and how these drive automated
versioning. `fix:` and `feat:` commits on `main` are what triggers a
release; get the type right.

## Pull requests

- Keep the diff focused — a bug fix doesn't need a drive-by refactor.
- Include or update tests for anything in `engine/`, `scripts/`, or
  `backtest/`.
- CI (`pytest`, a commit-message lint, and a Docker build smoke test) must
  pass before merge.
- If your change affects the live trading path (`engine/risk.py`,
  `engine/portfolio.py`, `scripts/run_daily.py`, or config), say so in the
  PR description and note what dry-run/backtest verification you ran.
