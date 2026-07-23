"""SEC insider-transaction (Form 4) ingestion.

Uses the SEC's bulk quarterly Form 3/4/5 data sets rather than fetching filings
one at a time. Per-filing retrieval would be millions of requests against a
10 req/sec limit; the bulk sets are ~13MB per quarter.

    https://www.sec.gov/files/structureddata/data/insider-transactions-data-sets/

Point-in-time discipline: signals are keyed on **FILING_DATE**, the date the
transaction became public, never on TRANS_DATE. An insider may buy on the 1st
and disclose on the 3rd; trading the 1st is lookahead, and lookahead is exactly
what makes an insider backtest look profitable when it is not.

Transaction codes that matter:
    P  open-market purchase   <- the signal
    S  open-market sale
    A  grant/award            (compensation, not a view)
    M  option exercise        (mechanical, not a view)
    F  tax withholding        (mechanical)

`AFF10B5ONE` flags a trade made under a pre-scheduled 10b5-1 plan. Cohen,
Malloy & Pomorski found the alpha sits in *opportunistic* trades, not routine
scheduled ones, so that flag is a first-class filter here.
"""

from __future__ import annotations

import datetime as dt
import io
import zipfile
from pathlib import Path

import pandas as pd
import requests

from engine.data import REPO_ROOT

BULK_URL = (
    "https://www.sec.gov/files/structureddata/data/"
    "insider-transactions-data-sets/{year}q{q}_form345.zip"
)
CACHE_DIR = REPO_ROOT / "state" / "edgar"
USER_AGENT = "austin austinwinstanley@hey.com"

BUY_CODE = "P"
SELL_CODE = "S"


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"})
    return s


def download_quarter(year: int, quarter: int, *, force: bool = False) -> Path | None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    dest = CACHE_DIR / f"{year}q{quarter}_form345.zip"
    if dest.exists() and not force and dest.stat().st_size > 10_000:
        return dest
    url = BULK_URL.format(year=year, q=quarter)
    r = _session().get(url, timeout=180)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    dest.write_bytes(r.content)
    return dest


def _read_tsv(z: zipfile.ZipFile, name: str, usecols: list[str]) -> pd.DataFrame:
    """Read a TSV, tolerating columns that don't exist in older quarters.

    The Form 345 schema evolved: `AFF10B5ONE` (the 10b5-1 plan flag) is absent
    before roughly 2012. Missing columns come back empty rather than raising,
    so early years still parse — they just can't use that filter.
    """
    with z.open(name) as fh:
        raw = fh.read()
    available = pd.read_csv(
        io.BytesIO(raw), sep="\t", nrows=0, dtype=str, low_memory=False
    ).columns
    present = [c for c in usecols if c in available]
    df = pd.read_csv(
        io.BytesIO(raw), sep="\t", usecols=present,
        dtype=str, on_bad_lines="skip", low_memory=False,
    )
    for missing in set(usecols) - set(present):
        df[missing] = pd.NA
    return df


def parse_quarter(path: Path) -> pd.DataFrame:
    """One row per (filing, insider, non-derivative transaction)."""
    with zipfile.ZipFile(path) as z:
        sub = _read_tsv(z, "SUBMISSION.tsv", [
            "ACCESSION_NUMBER", "FILING_DATE", "DOCUMENT_TYPE",
            "ISSUERCIK", "ISSUERNAME", "ISSUERTRADINGSYMBOL", "AFF10B5ONE",
        ])
        trans = _read_tsv(z, "NONDERIV_TRANS.tsv", [
            "ACCESSION_NUMBER", "TRANS_DATE", "TRANS_CODE", "TRANS_SHARES",
            "TRANS_PRICEPERSHARE", "TRANS_ACQUIRED_DISP_CD",
        ])
        owner = _read_tsv(z, "REPORTINGOWNER.tsv", [
            "ACCESSION_NUMBER", "RPTOWNERCIK", "RPTOWNERNAME",
            "RPTOWNER_RELATIONSHIP", "RPTOWNER_TITLE",
        ])

    sub = sub[sub["DOCUMENT_TYPE"] == "4"]
    df = trans.merge(sub, on="ACCESSION_NUMBER", how="inner").merge(
        owner, on="ACCESSION_NUMBER", how="left"
    )

    df["filing_date"] = pd.to_datetime(df["FILING_DATE"], format="%d-%b-%Y", errors="coerce")
    df["trans_date"] = pd.to_datetime(df["TRANS_DATE"], format="%d-%b-%Y", errors="coerce")
    df["shares"] = pd.to_numeric(df["TRANS_SHARES"], errors="coerce")
    df["price"] = pd.to_numeric(df["TRANS_PRICEPERSHARE"], errors="coerce")
    df["value"] = df["shares"] * df["price"]
    df["symbol"] = df["ISSUERTRADINGSYMBOL"].str.strip().str.upper()
    df["is_10b5_1"] = df["AFF10B5ONE"].fillna("0").isin(["1", "true", "TRUE"])

    rel = df["RPTOWNER_RELATIONSHIP"].fillna("")
    title = df["RPTOWNER_TITLE"].fillna("").str.upper()
    df["is_officer"] = rel.str.contains("Officer", case=False, na=False)
    df["is_director"] = rel.str.contains("Director", case=False, na=False)
    df["is_ten_pct"] = rel.str.contains("TenPercent", case=False, na=False)
    df["is_c_suite"] = title.str.contains(
        r"\bCEO\b|CHIEF EXEC|\bCFO\b|CHIEF FINANC|PRESIDENT|\bCOO\b|CHIEF OPERAT", regex=True, na=False
    )

    # Ticker hygiene: the field is free text and carries placeholders ("NONE",
    # "N/A"), blanks, and junk. A bad ticker silently joins to no price data,
    # which looks like "no signal" rather than "bad row".
    df = df[
        df["symbol"].notna()
        & df["symbol"].str.fullmatch(r"[A-Z][A-Z.\-]{0,6}", na=False)
        & ~df["symbol"].isin({"NONE", "N", "NA", "NULL", "NONE."})
    ]

    keep = [
        "ACCESSION_NUMBER", "filing_date", "trans_date", "TRANS_CODE",
        "TRANS_ACQUIRED_DISP_CD", "shares", "price", "value", "symbol",
        "ISSUERNAME", "is_10b5_1", "RPTOWNERCIK", "RPTOWNERNAME",
        "RPTOWNER_TITLE", "is_officer", "is_director", "is_ten_pct", "is_c_suite",
    ]
    df = df[keep].rename(columns={
        "TRANS_CODE": "code", "TRANS_ACQUIRED_DISP_CD": "acq_disp",
        "ISSUERNAME": "issuer", "RPTOWNERCIK": "owner_cik", "RPTOWNERNAME": "owner_name",
    })
    return df.dropna(subset=["filing_date", "symbol"])


def build_insider_dataset(
    start_year: int = 2010, end_year: int | None = None, *, refresh: bool = False
) -> pd.DataFrame:
    """Download and parse every quarter into one tidy frame (cached to parquet)."""
    end_year = end_year or dt.date.today().year
    out_path = CACHE_DIR / f"insider_{start_year}_{end_year}.parquet"
    if out_path.exists() and not refresh:
        return pd.read_parquet(out_path)

    frames = []
    for year in range(start_year, end_year + 1):
        for q in (1, 2, 3, 4):
            path = download_quarter(year, q)
            if path is None:
                continue
            try:
                frames.append(parse_quarter(path))
                print(f"  {year}Q{q}: {len(frames[-1]):>7,} transaction rows", flush=True)
            except Exception as exc:
                print(f"  {year}Q{q}: FAILED {type(exc).__name__}: {exc}", flush=True)

    if not frames:
        raise RuntimeError("no quarters parsed")
    df = pd.concat(frames, ignore_index=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    df.to_parquet(out_path)
    return df


# --------------------------------------------------------------------------
# Signal construction
# --------------------------------------------------------------------------


def open_market_buys(
    df: pd.DataFrame,
    *,
    exclude_10b5_1: bool = True,
    min_value: float = 50_000.0,
    max_value: float | None = 20_000_000.0,
    insiders_only: bool = True,
) -> pd.DataFrame:
    """Open-market purchases only — the subset with documented predictive content.

    `insiders_only` drops filings where the buyer is *solely* a 10% owner. Those
    are usually PE/VC funds taking down a PIPE or financing round, which is a
    capital-structure event rather than a manager forming a view — and they
    dominate the dollar rankings if left in (a $150M round-number "purchase" by
    three non-officers is a placement, not conviction).

    `max_value` drops the same thing from the other direction: a purchase larger
    than any plausible personal position.
    """
    buys = df[(df["code"] == BUY_CODE) & (df["acq_disp"] == "A")].copy()
    if exclude_10b5_1:
        buys = buys[~buys["is_10b5_1"]]
    if insiders_only:
        buys = buys[buys["is_officer"] | buys["is_director"]]
    buys = buys[buys["value"] >= min_value]
    if max_value is not None:
        buys = buys[buys["value"] <= max_value]
    return buys


def cluster_signals(
    buys: pd.DataFrame,
    *,
    window_days: int = 30,
    min_insiders: int = 3,
    require_c_suite: bool = False,
) -> pd.DataFrame:
    """Detect cluster buys: N+ distinct insiders buying the same issuer in a window.

    Keyed on filing_date, so the signal fires the day the market could actually
    have seen it. Cluster buys historically produce roughly double the excess
    return of single-insider purchases.
    """
    if require_c_suite:
        eligible = buys[buys["is_c_suite"]]["symbol"].unique()
        buys = buys[buys["symbol"].isin(eligible)]

    buys = buys.sort_values("filing_date")
    rows = []
    for symbol, grp in buys.groupby("symbol", sort=False):
        grp = grp.sort_values("filing_date")
        dates = grp["filing_date"].to_numpy()
        for i in range(len(grp)):
            lo = dates[i] - pd.Timedelta(days=window_days)
            win = grp.iloc[max(0, i - 200) : i + 1]
            win = win[win["filing_date"] >= lo]
            n_insiders = win["owner_cik"].nunique()
            if n_insiders >= min_insiders:
                rows.append({
                    "symbol": symbol,
                    "signal_date": grp.iloc[i]["filing_date"],
                    "n_insiders": int(n_insiders),
                    "total_value": float(win["value"].sum()),
                    "n_c_suite": int(win["is_c_suite"].sum()),
                    "issuer": grp.iloc[i]["issuer"],
                })
    if not rows:
        return pd.DataFrame(columns=["symbol", "signal_date", "n_insiders", "total_value", "n_c_suite", "issuer"])

    out = pd.DataFrame(rows).sort_values(["symbol", "signal_date"])
    # One signal per symbol per window — otherwise every subsequent filing in
    # the same cluster re-fires the same event.
    out["prev"] = out.groupby("symbol")["signal_date"].shift(1)
    out["gap"] = (out["signal_date"] - out["prev"]).dt.days
    out = out[out["prev"].isna() | (out["gap"] > window_days)]
    return out.drop(columns=["prev", "gap"]).reset_index(drop=True)
