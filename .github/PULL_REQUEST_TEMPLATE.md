## What and why

## Testing

- [ ] `pytest` passes
- [ ] If this touches `engine/`, `scripts/`, or config: both dry-runs pass
      (`scripts.run_daily --dry-run --force`, base and `--profile 2x`)
- [ ] If this is a strategy/risk-control change: backtest evidence attached
      or linked (`reports/*.json` with a `decision` field — see `AGENTS.md`)

## Commit messages

Uses [Conventional Commits](https://www.conventionalcommits.org/) (`feat:`/`fix:`/
`chore:`/`docs:`/...) — see `AGENTS.md`'s "Commit messages" section. `fix:`/`feat:`
drive the next release version.

## Live trading path

Does this change `engine/risk.py`, `engine/portfolio.py`,
`scripts/run_daily.py`, `config.yaml`, or `config_2x.yaml`? If so, describe
what dry-run/backtest verification you ran — these are the paths that can
actually reach the broker.
