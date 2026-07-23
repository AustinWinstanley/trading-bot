"""13F ingestion: clone what top fundamental funds actually hold.

Data: SEC bulk 13F data sets (free). INFOTABLE is ~400MB/quarter, so it is
streamed against a curated CIK whitelist rather than loaded.

The academic prior (Cohen-Polk-Silli "Best Ideas"; Martijn Cremers' active
share work): the *largest, most concentrated* positions of skilled managers
outperform, even observed at the mandated 45-day lag. The lag kills fast
signals but concentrated fundamental positions are held for years, so the
information decays slowly.

CUSIP -> ticker uses SEC fails-to-deliver files, which carry both columns and
are free — the only free CUSIP map that exists.

POINT-IN-TIME: holdings become tradeable on FILING_DATE + 1, never on the
period end. The 45-day lag is the whole game; hiding it manufactures returns.
"""

from __future__ import annotations

import datetime as dt
import io
import re
import zipfile
from pathlib import Path

import pandas as pd
import requests

from engine.data import REPO_ROOT

CACHE_DIR = REPO_ROOT / "state" / "thirteenf"
USER_AGENT = "austin austinwinstanley@hey.com"
LIST_URL = "https://www.sec.gov/data-research/sec-markets-data/form-13f-data-sets"

# Concentrated, fundamental, long-horizon managers — the profile the research
# says survives the 45-day lag. Chosen by reputation BEFORE seeing results
# (no peeking at which clone best — that would be selection on the outcome).
FUNDS = {
    # CIKs verified against COVERPAGE.FILINGMANAGER_NAME in the actual data —
    # the first draft used from-memory CIKs and silently pulled in BlackRock
    # (78k index positions) labelled as "Lone Pine". Verify, never recall.
    1067983: "Berkshire Hathaway",
    1336528: "Pershing Square",
    1029160: "Soros Fund Management",
    1656456: "Appaloosa",
    1061165: "Lone Pine Capital",
    1040273: "Third Point",
    1167483: "Tiger Global",
    921669:  "Icahn",
    1103804: "Viking Global",
    1418814: "ValueAct",
    1649339: "Scion Asset Management",
    1054420: "Baupost Group",
}


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"})
    return s


def list_dataset_urls() -> list[str]:
    r = _session().get(LIST_URL, timeout=120)
    r.raise_for_status()
    paths = re.findall(r'/files/structureddata/data/form-13f-data-sets/[^"]+\.zip', r.text)
    return [f"https://www.sec.gov{p}" for p in dict.fromkeys(paths)]


def download_all(*, since_year: int = 2021) -> list[Path]:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    out = []
    for url in list_dataset_urls():
        name = url.rsplit("/", 1)[-1]
        year_hits = [int(y) for y in re.findall(r"(20\d\d)", name)]
        if not year_hits or max(year_hits) < since_year:
            continue
        dest = CACHE_DIR / name
        if not dest.exists() or dest.stat().st_size < 100_000:
            r = _session().get(url, timeout=600)
            r.raise_for_status()
            dest.write_bytes(r.content)
            print(f"  downloaded {name} ({len(r.content)/1e6:.0f}MB)", flush=True)
        out.append(dest)
    return out


def _member(z: zipfile.ZipFile, name: str) -> str:
    """Some quarters nest members in a subdirectory (01JUN2025.../INFOTABLE.tsv);
    resolve by basename, case-insensitively."""
    for n in z.namelist():
        if n.rsplit("/", 1)[-1].upper() == name.upper():
            return n
    raise KeyError(f"{name} not in {path_hint(z)}")


def path_hint(z: zipfile.ZipFile) -> str:
    return getattr(z, "filename", "?") or "?"


def parse_dataset(path: Path, cik_whitelist: set[int]) -> pd.DataFrame:
    """Holdings of whitelisted funds only, streamed from INFOTABLE."""
    with zipfile.ZipFile(path) as z:
        with z.open(_member(z, "SUBMISSION.tsv")) as fh:
            sub = pd.read_csv(io.BytesIO(fh.read()), sep="\t", dtype=str, low_memory=False)
        sub["cik_int"] = pd.to_numeric(sub["CIK"], errors="coerce")
        sub = sub[sub["cik_int"].isin(cik_whitelist)]
        sub = sub[sub["SUBMISSIONTYPE"].isin(["13F-HR", "13F-HR/A"])]
        wanted = dict(zip(sub["ACCESSION_NUMBER"], sub["cik_int"]))
        if not wanted:
            return pd.DataFrame()

        keep = []
        with z.open(_member(z, "INFOTABLE.tsv")) as fh:
            header = fh.readline().decode(errors="replace").rstrip("\n").split("\t")
            idx = {c: i for i, c in enumerate(header)}
            i_acc, i_cusip = idx["ACCESSION_NUMBER"], idx["CUSIP"]
            i_val, i_shares = idx["VALUE"], idx["SSHPRNAMT"]
            i_type, i_putcall = idx["SSHPRNAMTTYPE"], idx["PUTCALL"]
            i_name = idx["NAMEOFISSUER"]
            for line in fh:
                parts = line.decode(errors="replace").rstrip("\n").split("\t")
                if len(parts) <= i_putcall or parts[i_acc] not in wanted:
                    continue
                if parts[i_putcall].strip():      # skip option positions
                    continue
                if parts[i_type] != "SH":         # shares only, no principal amounts
                    continue
                keep.append((parts[i_acc], parts[i_cusip], parts[i_name],
                             parts[i_val], parts[i_shares]))

    if not keep:
        return pd.DataFrame()
    df = pd.DataFrame(keep, columns=["accession", "cusip", "issuer", "value", "shares"])
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    df["shares"] = pd.to_numeric(df["shares"], errors="coerce")
    df["cik"] = df["accession"].map(wanted)

    meta = sub.set_index("ACCESSION_NUMBER")
    df["filing_date"] = pd.to_datetime(df["accession"].map(meta["FILING_DATE"]),
                                       format="%d-%b-%Y", errors="coerce")
    df["period"] = pd.to_datetime(df["accession"].map(meta["PERIODOFREPORT"]),
                                  format="%d-%b-%Y", errors="coerce")
    return df.dropna(subset=["filing_date", "cusip", "value"])


def build_holdings(*, since_year: int = 2021, refresh: bool = False) -> pd.DataFrame:
    out_path = CACHE_DIR / "holdings.parquet"
    if out_path.exists() and not refresh:
        return pd.read_parquet(out_path)
    frames = []
    for path in download_all(since_year=since_year):
        f = parse_dataset(path, set(FUNDS))
        if len(f):
            frames.append(f)
            print(f"  {path.name}: {len(f):,} positions from "
                  f"{f['cik'].nunique()} funds", flush=True)
    df = pd.concat(frames, ignore_index=True)
    # Amendments: keep the latest filing per (fund, period).
    df = df.sort_values("filing_date").drop_duplicates(
        ["cik", "period", "cusip"], keep="last")
    df.to_parquet(out_path)
    return df


# --------------------------------------------------------------------------
# CUSIP -> ticker via fails-to-deliver files
# --------------------------------------------------------------------------


FTD_URL = "https://www.sec.gov/files/data/fails-deliver-data/cnsfails{ym}{half}.zip"


def cusip_ticker_map(*, months: int = 18, refresh: bool = False) -> pd.DataFrame:
    """CUSIP(9) -> ticker from recent FTD files. Free and surprisingly complete:
    any security that ever fails to deliver (i.e. essentially all of them at
    some point) appears with both identifiers."""
    out_path = CACHE_DIR / "cusip_map.parquet"
    if out_path.exists() and not refresh:
        return pd.read_parquet(out_path)

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    sess = _session()
    rows = []
    today = dt.date.today()
    for k in range(months):
        d = dt.date(today.year, today.month, 1) - dt.timedelta(days=30 * k)
        for half in ("a", "b"):
            url = FTD_URL.format(ym=d.strftime("%Y%m"), half=half)
            try:
                r = sess.get(url, timeout=180)
                if r.status_code != 200:
                    continue
                z = zipfile.ZipFile(io.BytesIO(r.content))
                with z.open(z.namelist()[0]) as fh:
                    ftd = pd.read_csv(fh, sep="|", dtype=str, on_bad_lines="skip",
                                      encoding_errors="replace")
                cols = {c.upper(): c for c in ftd.columns}
                if "CUSIP" in cols and "SYMBOL" in cols:
                    rows.append(ftd[[cols["CUSIP"], cols["SYMBOL"]]]
                                .rename(columns={cols["CUSIP"]: "cusip",
                                                 cols["SYMBOL"]: "symbol"}))
            except Exception:
                continue

    m = pd.concat(rows, ignore_index=True).dropna()
    m["symbol"] = m["symbol"].str.strip().str.upper()
    m = m[m["symbol"].str.fullmatch(r"[A-Z][A-Z.\-]{0,6}", na=False)]
    m = m.drop_duplicates("cusip")
    m.to_parquet(out_path)
    return m
