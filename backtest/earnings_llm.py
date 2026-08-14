"""The LLM earnings layer, tested as a measurable signal.

Hypothesis (from the original PEAD design, never yet tested): a volume-confirmed
earnings gap continues only when the release contains a genuine forward
improvement — raised guidance, structural margin gain — and fades when the beat
is cosmetic (tax item, buyback EPS, one-time gain). Separating those is a
reading-comprehension task, which is what an LLM is actually good at.

The experiment:
  1. Take gap-up events (>=5% on 2x volume, liquid, >=$5) from the price matrices.
  2. For each, find the company's 8-K filed within [-1, +1] days of the gap via
     EDGAR; require item 2.02 (results of operations). Non-earnings gaps drop out.
  3. Claude reads the press-release exhibit and answers a fixed rubric:
     guidance_raised / beat_quality / one_off_driver -> confirm true/false.
  4. Compare forward 20/60-day benchmark-adjusted returns of CONFIRMED vs
     REJECTED events. If the split is not significant, the layer adds nothing.

Contamination caveat, stated up front: the classifier model's training data
covers these years, so it may "know" what happened next to these companies.
The rubric forces extraction ("does THIS TEXT raise guidance?") rather than
prediction, which limits but does not eliminate hindsight leakage. A clean
test requires events after the model's cutoff — i.e. the forward paper-trade.
This backtest is evidence, not proof.

Usage:
    python -m backtest.earnings_llm prepare   --sample 240
    python -m backtest.earnings_llm classify  --parallel 3
    python -m backtest.earnings_llm analyze
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import pandas as pd
import requests

from backtest.drift_study import event_mask
from backtest.xsec_data import load as xload
from engine.data import REPO_ROOT, user_agent
from engine.fundamentals import cik_map

STATE = REPO_ROOT / "state" / "earnings_llm"
HORIZONS = (5, 20, 60)

PROMPT = """You are analysing a company's earnings press release. Judge ONLY what this text says — do not use any outside knowledge about the company or what happened later.

Answer these questions about the text, then give a verdict:
1. guidance_raised: does the release RAISE forward guidance (revenue or EPS outlook above prior guidance), or initiate guidance above analyst-framing? (true/false)
2. beat_quality: is reported strength driven by core operations (revenue growth, margin expansion) rather than items? ("core" | "mixed" | "one_off")
3. one_off_driver: is the headline number materially helped by a tax item, asset sale, buyback-driven EPS, accounting change, or other one-time factor? (true/false)
4. red_flags: any going-concern language, restatement, guidance WITHDRAWN or lowered, or major customer loss? (true/false)

verdict rule (apply mechanically):
confirm = guidance_raised AND beat_quality=="core" AND NOT one_off_driver AND NOT red_flags

Respond with ONLY a JSON object, no other text:
{"guidance_raised": bool, "beat_quality": "core"|"mixed"|"one_off", "one_off_driver": bool, "red_flags": bool, "confirm": bool, "one_line_reason": "..."}

=== PRESS RELEASE TEXT ===
"""


# --------------------------------------------------------------------------
# prepare: events -> matching 8-K text
# --------------------------------------------------------------------------


def _sec_get(url: str, *, tries: int = 3) -> requests.Response | None:
    for i in range(tries):
        try:
            r = requests.get(url, headers={"User-Agent": user_agent()}, timeout=60)
            if r.status_code == 200:
                return r
            if r.status_code in (403, 429):
                time.sleep(2 + i * 2)
        except requests.RequestException:
            time.sleep(1 + i)
    return None


def find_8k(cik: int, gap_date: dt.date) -> dict | None:
    """The 8-K with item 2.02 filed within [-1, +1] of the gap, else None."""
    r = _sec_get(f"https://data.sec.gov/submissions/CIK{cik:010d}.json")
    if r is None:
        return None
    recent = r.json().get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    for i, form in enumerate(forms):
        if form != "8-K":
            continue
        fdate = dt.date.fromisoformat(recent["filingDate"][i])
        if abs((fdate - gap_date).days) > 1:
            continue
        items = recent.get("items", [""] * len(forms))[i] or ""
        if "2.02" not in items:
            continue
        return {
            "accession": recent["accessionNumber"][i].replace("-", ""),
            "primary": recent["primaryDocument"][i],
            "filing_date": fdate.isoformat(),
            "items": items,
        }
    return None


def fetch_release_text(cik: int, accession: str, primary: str, *, max_chars: int = 14000) -> str | None:
    """Prefer the EX-99.* press release exhibit; fall back to the 8-K body."""
    base = f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession}"
    r = _sec_get(f"{base}/index.json")
    doc = primary
    if r is not None:
        items = r.json().get("directory", {}).get("item", [])
        for it in items:
            name = it.get("name", "")
            if re.search(r"ex[-_]?99", name, re.I) and name.lower().endswith((".htm", ".html")):
                doc = name
                break
    r = _sec_get(f"{base}/{doc}")
    if r is None:
        return None
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", r.text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(re.sub(r"\s+", " ", text)).strip()
    if len(text) < 500:
        return None
    return text[:max_chars]


def prepare(sample: int) -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    close, volume = xload()
    mask = event_mask(close, volume, min_move=0.05, min_vol_mult=2.0,
                      min_price=5.0, min_dollar_volume=2e7, direction="up")
    events = mask.stack()
    events = events[events].reset_index()
    events.columns = ["date", "symbol", "_"]
    events["date"] = pd.DatetimeIndex(events["date"]).tz_localize(None).normalize()
    # Spread the sample across time rather than clustering in one quarter.
    events = events.sort_values("date")
    step = max(len(events) // sample, 1)
    picked = events.iloc[::step].head(sample)
    print(f"{len(events):,} candidate events; sampling {len(picked)}")

    cmap = cik_map().set_index("symbol")["cik"]
    out_path = STATE / "events.jsonl"
    n_done = 0
    seen = set()
    if out_path.exists():
        for line in out_path.read_text().splitlines():
            seen.add(json.loads(line)["event_id"])
    with out_path.open("a") as fh:
        for _, ev in picked.iterrows():
            event_id = f"{ev.symbol}_{ev.date.date()}"
            if event_id in seen:
                continue
            cik = cmap.get(ev.symbol)
            if pd.isna(cik):
                continue
            meta = find_8k(int(cik), ev.date.date())
            time.sleep(0.12)              # SEC: stay under 10 req/s
            if meta is None:
                continue
            text = fetch_release_text(int(cik), meta["accession"], meta["primary"])
            time.sleep(0.12)
            if text is None:
                continue
            fh.write(json.dumps({
                "event_id": event_id, "symbol": ev.symbol,
                "gap_date": str(ev.date.date()), **meta, "text": text,
            }) + "\n")
            fh.flush()
            n_done += 1
            print(f"  [{n_done}] {event_id} 8-K items={meta['items']}", flush=True)
    print(f"prepared {n_done} new events -> {out_path}")


# --------------------------------------------------------------------------
# classify: one claude -p call per event
# --------------------------------------------------------------------------


def _classify_one(ev: dict, model: str) -> dict:
    prompt = PROMPT + ev["text"]
    try:
        proc = subprocess.run(
            ["claude", "-p", "--model", model, "--max-turns", "1"],
            input=prompt, capture_output=True, text=True, timeout=240,
        )
        raw = proc.stdout.strip()
        m = re.search(r"\{.*\}", raw, re.S)
        verdict = json.loads(m.group(0)) if m else {"error": f"no json in: {raw[:200]}"}
    except subprocess.TimeoutExpired:
        verdict = {"error": "timeout"}
    except Exception as exc:
        verdict = {"error": f"{type(exc).__name__}: {exc}"}
    return {"event_id": ev["event_id"], "symbol": ev["symbol"],
            "gap_date": ev["gap_date"], "verdict": verdict}


def classify(parallel: int, model: str) -> None:
    events = [json.loads(l) for l in (STATE / "events.jsonl").read_text().splitlines()]
    out_path = STATE / "verdicts.jsonl"
    done = set()
    if out_path.exists():
        done = {json.loads(l)["event_id"] for l in out_path.read_text().splitlines()}
    todo = [e for e in events if e["event_id"] not in done]
    print(f"{len(todo)} events to classify ({len(done)} already done)")

    with out_path.open("a") as fh, ThreadPoolExecutor(max_workers=parallel) as pool:
        futures = {pool.submit(_classify_one, ev, model): ev["event_id"] for ev in todo}
        for i, fut in enumerate(as_completed(futures), 1):
            row = fut.result()
            fh.write(json.dumps(row) + "\n")
            fh.flush()
            v = row["verdict"]
            tag = "ERR" if "error" in v else ("CONFIRM" if v.get("confirm") else "reject")
            print(f"  [{i}/{len(todo)}] {row['event_id']:24} {tag}", flush=True)


# --------------------------------------------------------------------------
# analyze: does the verdict separate forward returns?
# --------------------------------------------------------------------------


def analyze() -> None:
    verdicts = [json.loads(l) for l in (STATE / "verdicts.jsonl").read_text().splitlines()]
    ok = [v for v in verdicts if "error" not in v["verdict"]]
    print(f"{len(ok)} classified ({len(verdicts) - len(ok)} errors)")

    close, _ = xload()
    close = close.copy()
    # Third occurrence of the same trap: Alpaca index is tz-aware UTC at
    # 04:00/05:00, verdict gap_dates are naive. Normalise AND drop tz so
    # searchsorted compares like with like.
    close.index = pd.DatetimeIndex(close.index).tz_convert("UTC").tz_localize(None).normalize()
    close = close[~close.index.duplicated(keep="last")]
    spy = close["SPY"]

    rows = []
    for v in ok:
        sym, date = v["symbol"], pd.Timestamp(v["gap_date"])
        if sym not in close.columns:
            continue
        px, b = close[sym], spy
        idx = px.index.searchsorted(date, side="right")   # enter close of D+1
        if idx + 1 >= len(px):
            continue
        entry, b_entry = px.iloc[idx], b.iloc[idx]
        row = {"event_id": v["event_id"], "confirm": bool(v["verdict"].get("confirm")),
               "guidance_raised": bool(v["verdict"].get("guidance_raised"))}
        okrow = True
        for h in HORIZONS:
            j = idx + h
            if j >= len(px) or pd.isna(px.iloc[j]) or pd.isna(entry) or entry == 0:
                okrow = False
                break
            row[f"car{h}"] = float(px.iloc[j] / entry - 1) - float(b.iloc[j] / b_entry - 1)
        if okrow:
            rows.append(row)

    df = pd.DataFrame(rows)
    print(f"{len(df)} events with full forward prices; "
          f"{df['confirm'].sum()} confirmed / {(~df['confirm']).sum()} rejected\n")

    out = []
    for h in HORIZONS:
        c, r = df[df.confirm][f"car{h}"], df[~df.confirm][f"car{h}"]
        if len(c) < 8 or len(r) < 8:
            continue
        pooled = np.sqrt(c.var(ddof=1) / len(c) + r.var(ddof=1) / len(r))
        t = float((c.mean() - r.mean()) / pooled) if pooled > 0 else 0.0
        out.append({"horizon_days": h,
                    "confirmed_mean_car": round(float(c.mean()), 4),
                    "rejected_mean_car": round(float(r.mean()), 4),
                    "spread": round(float(c.mean() - r.mean()), 4),
                    "n_confirmed": len(c), "n_rejected": len(r),
                    "t_stat": round(t, 2), "significant": abs(t) > 1.96})
    res = pd.DataFrame(out)
    pd.set_option("display.width", 160)
    print(res.to_string(index=False))
    Path("reports/earnings_llm.json").write_text(json.dumps({
        "note": "training-data contamination possible; rubric is extractive to limit it",
        "results": out, "n_events": len(df),
    }, indent=2))
    print("\nWrote reports/earnings_llm.json")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["prepare", "classify", "analyze"])
    ap.add_argument("--sample", type=int, default=240)
    ap.add_argument("--parallel", type=int, default=3)
    ap.add_argument("--model", default="claude-sonnet-5")
    args = ap.parse_args()
    if args.cmd == "prepare":
        prepare(args.sample)
    elif args.cmd == "classify":
        classify(args.parallel, args.model)
    else:
        analyze()


if __name__ == "__main__":
    main()
