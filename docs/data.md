# Rebuilding cached market data

Backtests and research studies read cached parquet/SQLite files under
`state/`. Almost none of that cache is tracked in git — it is rebuildable
from a data provider, and each provider's terms restrict redistributing
their data. This repo previously tracked a small number of Tiingo- and
SEC-EDGAR-derived parquet files as a convenience; they were removed from
the repository (tree and history) so the public repo never redistributes
third-party market data. This page is how to rebuild them.

## Tiingo daily bars — `state/history/`, `state/history_assets/`

`engine/tiingo.py` is the client. Tiingo's free tier gives 30+ years of
split/dividend-adjusted daily bars at 50 symbols/hour — enough to cover
this project's universes in one pull.

1. Get a free key at <https://www.tiingo.com/account/api/token> and set
   `TIINGO_API_TOKEN` in `.env`.
2. **Core backtest universe** (`state/history/`) — momentum/leveraged
   symbols plus SPY, driven by `config.yaml`:

   ```bash
   .venv/bin/python -m backtest.run --source tiingo --start 2015-01-01 --fetch
   ```

3. **TSMOM asset universe** (`state/history_assets/`) — the symbols in
   `config.yaml`'s `sleeves_paper.tsmom_universe` (currently `GLD, SLV,
   DBC, USO, UNG, DBA, TLT, IEF, LQD, HYG, EMB, VNQ, UUP, FXE, FXY`):

   ```bash
   .venv/bin/python - <<'EOF'
   from pathlib import Path
   from engine.config import load_config
   from engine.tiingo import TiingoClient, save_parquet

   cfg = load_config("config.yaml")
   symbols = cfg.sleeves_paper["tsmom_universe"]
   client = TiingoClient()
   client.check_auth()
   frames = client.get_bars(symbols, __import__("datetime").date(2010, 1, 1),
                             __import__("datetime").date.today())
   save_parquet(frames, Path("state/history_assets"))
   EOF
   ```

Both directories are read through `engine.tiingo.load_parquet` /
`save_parquet`, so any script that already reads one of them will pick up
a freshly rebuilt cache with no further changes.

## SEC EDGAR — `state/thirteenf/`, `state/edgar/`, `state/fundamentals/`

`engine/thirteenf.py`, `engine/edgar.py`, and `engine/fundamentals.py`
stream SEC's free bulk data sets. Every request needs a contact-identifying
`User-Agent`: set `SEC_USER_AGENT` in `.env` (e.g. `"Your Name
you@example.com"`) — see [SEC's developer FAQ](https://www.sec.gov/os/webmaster-faq#developers).
Requests without one get HTTP 403.

13F holdings clone (`state/thirteenf/holdings.parquet`,
`state/thirteenf/cusip_map.parquet`):

```bash
.venv/bin/python - <<'EOF'
from engine.thirteenf import build_holdings, cusip_ticker_map
build_holdings()
cusip_ticker_map()
EOF
```

Insider Form 3/4/5 bulk sets and XBRL fundamentals cache the same way —
see `engine/edgar.py:download_quarter` and
`engine/fundamentals.py:download_quarter`/`build`; both cache to
`state/edgar/` and `state/fundamentals/` respectively and are already
gitignored.

## Everything else under `state/`

Every other subdirectory `.gitignore` excludes (`state/xsec/`,
`state/crypto/`, `state/options/`, `state/french/`, `state/cboe/`, ...) is
populated the same way: read the module's docstring for the source and
entry point, then run it once locally. None of it needs to be committed —
these caches exist purely so repeated backtest runs don't refetch the same
history.
