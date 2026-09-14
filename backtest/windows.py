"""The evaluation windows every study slices on, defined once.

Studies used to spell these out locally (`slice(None, "2022-12-31")` here,
`"2023-01-01"` there), which is how the held-out window's end date quietly
diverged from the AGENTS.md freeze policy. Import from here instead.

- `EARLY`   — the 2020-2022 screen (first return day of the cached panel
              through the last 2022 session).
- `HELDOUT` — the 2023+ screen. It now ENDS on 2026-08-12: per AGENTS.md
              ("The 2026-08-04 frozen window was substantially spent by
              2026-08-12"), 2026-08-13 onward is the frozen final-validation
              window that no study may tune on. `heldout_2023_plus` is itself
              no longer a clean hold-out — treat EARLY + HELDOUT as a screen a
              candidate must clear, not as sufficient evidence.
- `FROZEN_START` — first day of the frozen window. Data from here on is for
              forward observation and final validation only.
- `STRESS_WINDOWS` — the long-history stress episodes, re-exported from
              `backtest.long_history_stress_study` so there is one source.
"""

from __future__ import annotations

import pandas as pd

from backtest.long_history_stress_study import STRESS_WINDOWS

EARLY = ("2020-07-28", "2022-12-30")
HELDOUT = ("2023-01-03", "2026-08-12")
FROZEN_START = "2026-08-13"

# The (window label -> bounds) mapping most studies iterate when building
# `passes_gate_all_cells` cells. Labels match the historical report keys.
SCREEN_WINDOWS = {
    "early_2020_2022": EARLY,
    "heldout_2023_plus": HELDOUT,
}

__all__ = [
    "EARLY",
    "HELDOUT",
    "FROZEN_START",
    "SCREEN_WINDOWS",
    "STRESS_WINDOWS",
    "slice_window",
]


def slice_window(obj: pd.Series | pd.DataFrame, window: tuple[str, str]):
    """Inclusive date slice of a DatetimeIndex-ed Series/DataFrame.

    `window` is `(start, end)` as ISO date strings (any of the constants
    above). Works on tz-aware and tz-naive indexes alike; rows are returned
    in index order and the result is a copy, so slicing then mutating never
    leaks back into the caller's frame.
    """
    start, end = window
    idx = pd.DatetimeIndex(obj.index)
    if not idx.is_monotonic_increasing:
        raise ValueError("slice_window needs a sorted DatetimeIndex")
    lo, hi = pd.Timestamp(start), pd.Timestamp(end)
    if idx.tz is not None:
        lo, hi = lo.tz_localize(idx.tz), hi.tz_localize(idx.tz)
    if lo > hi:
        raise ValueError(f"window start {start} is after end {end}")
    mask = (idx >= lo) & (idx <= hi)
    return obj.loc[mask].copy()
