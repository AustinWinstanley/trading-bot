"""Print the read-only base-versus-2x matched-fill timing observation."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

from engine.data import REPO_ROOT
from engine.execution_timing import size_regression_summary, timing_summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-db", type=Path, default=REPO_ROOT / "state/paper.db")
    parser.add_argument("--leveraged-db", type=Path, default=REPO_ROOT / "state/paper_2x.db")
    parser.add_argument("--since", default="2026-08-04")
    parser.add_argument("--minimum-pairs", type=int, default=100)
    parser.add_argument(
        "--size-regression", action="store_true",
        help="print the order-size-vs-schedule regression instead of the "
             "matched-pairs timing summary — see engine.execution_timing."
             "size_regression_summary",
    )
    args = parser.parse_args()
    if not args.base_db.exists() or not args.leveraged_db.exists():
        raise FileNotFoundError("both paper journals are required")
    with sqlite3.connect(args.base_db) as base, sqlite3.connect(
        args.leveraged_db
    ) as leveraged:
        if args.size_regression:
            result = size_regression_summary(base, leveraged, since=args.since)
        else:
            result = timing_summary(
                base,
                leveraged,
                since=args.since,
                control_min_pairs=args.minimum_pairs,
            )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
