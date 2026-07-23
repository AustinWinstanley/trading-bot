"""Schema-validated config loader.

An invalid config raises. It never falls back to defaults — a typo in a risk
limit must stop the bot, not silently loosen it.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = REPO_ROOT / "config.yaml"

VALID_MODES = {"paper", "live", "halt"}


class ConfigError(Exception):
    """Raised when the config is missing, malformed, or out of bounds."""


@dataclass(frozen=True)
class RiskLimits:
    max_position_pct: float
    max_leveraged_exposure_pct: float
    max_long_exposure_pct: float
    max_gross_exposure_pct: float
    max_positions: int
    stop_loss_pct: float
    stop_atr_multiple: float
    max_stop_distance_pct: float
    daily_loss_limit_pct: float
    monthly_kill_switch_pct: float
    peak_drawdown_halt_pct: float
    allow_averaging_down: bool
    loss_reentry_block_days: int
    max_short_exposure_pct: float


@dataclass(frozen=True)
class ExecutionLimits:
    order_type: str
    max_limit_slippage_pct: float
    no_entry_first_minutes: int
    no_entry_last_minutes: int
    market_open: dt.time
    market_close: dt.time
    timezone: str


@dataclass(frozen=True)
class UniverseFilters:
    min_price: float
    min_avg_dollar_volume: float
    exclude_ipo_days: int
    allow_short: bool
    inverse_etfs: tuple[str, ...]


@dataclass(frozen=True)
class Config:
    mode: str
    starting_equity: float
    risk: RiskLimits
    execution: ExecutionLimits
    universe: UniverseFilters
    sleeves: dict
    sleeves_paper: dict
    leveraged_symbols: frozenset[str]


def _require(mapping: dict, key: str, path: str):
    if key not in mapping:
        raise ConfigError(f"missing required key: {path}.{key}" if path else f"missing required key: {key}")
    return mapping[key]


def _fraction(value, name: str, *, low: float = 0.0, high: float = 1.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{name} must be a number, got {value!r}")
    value = float(value)
    if not (low < value <= high):
        raise ConfigError(f"{name} must be in ({low}, {high}], got {value}")
    return value


def _positive_int(value, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigError(f"{name} must be a positive integer, got {value!r}")
    return value


def _positive_number(value, name: str, *, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{name} must be a number, got {value!r}")
    value = float(value)
    if not (0 < value <= high):
        raise ConfigError(f"{name} must be in (0, {high}], got {value}")
    return value


def _parse_time(value, name: str) -> dt.time:
    try:
        hh, mm = str(value).split(":")
        return dt.time(int(hh), int(mm))
    except (ValueError, AttributeError) as exc:
        raise ConfigError(f"{name} must be HH:MM, got {value!r}") from exc


def load_config(path: Path | str | None = None) -> Config:
    path = Path(path) if path else DEFAULT_CONFIG_PATH
    if not path.exists():
        raise ConfigError(f"config not found: {path}")

    try:
        raw = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise ConfigError(f"config is not valid YAML: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError("config root must be a mapping")

    mode = _require(raw, "mode", "")
    if mode not in VALID_MODES:
        raise ConfigError(f"mode must be one of {sorted(VALID_MODES)}, got {mode!r}")

    account = _require(raw, "account", "")
    starting_equity = _require(account, "starting_equity", "account")
    if not isinstance(starting_equity, (int, float)) or starting_equity <= 0:
        raise ConfigError(f"account.starting_equity must be positive, got {starting_equity!r}")

    r = _require(raw, "risk", "")
    risk = RiskLimits(
        max_position_pct=_fraction(_require(r, "max_position_pct", "risk"), "risk.max_position_pct"),
        max_leveraged_exposure_pct=_fraction(
            _require(r, "max_leveraged_exposure_pct", "risk"), "risk.max_leveraged_exposure_pct"
        ),
        max_long_exposure_pct=_positive_number(
            _require(r, "max_long_exposure_pct", "risk"), "risk.max_long_exposure_pct", high=3.0
        ),
        max_gross_exposure_pct=_positive_number(
            _require(r, "max_gross_exposure_pct", "risk"), "risk.max_gross_exposure_pct", high=3.5
        ),
        max_positions=_positive_int(_require(r, "max_positions", "risk"), "risk.max_positions"),
        stop_loss_pct=_fraction(_require(r, "stop_loss_pct", "risk"), "risk.stop_loss_pct"),
        stop_atr_multiple=float(_require(r, "stop_atr_multiple", "risk")),
        max_stop_distance_pct=_fraction(
            _require(r, "max_stop_distance_pct", "risk"), "risk.max_stop_distance_pct"
        ),
        daily_loss_limit_pct=_fraction(
            _require(r, "daily_loss_limit_pct", "risk"), "risk.daily_loss_limit_pct"
        ),
        monthly_kill_switch_pct=_fraction(
            _require(r, "monthly_kill_switch_pct", "risk"), "risk.monthly_kill_switch_pct"
        ),
        peak_drawdown_halt_pct=_fraction(
            _require(r, "peak_drawdown_halt_pct", "risk"), "risk.peak_drawdown_halt_pct"
        ),
        allow_averaging_down=bool(_require(r, "allow_averaging_down", "risk")),
        loss_reentry_block_days=int(_require(r, "loss_reentry_block_days", "risk")),
        max_short_exposure_pct=float(r.get("max_short_exposure_pct", 0.0)),
    )

    # Averaging down is never permitted. Refuse to start rather than honour it.
    if risk.allow_averaging_down:
        raise ConfigError("risk.allow_averaging_down must be false — averaging down is never permitted")

    if risk.stop_atr_multiple <= 0:
        raise ConfigError("risk.stop_atr_multiple must be positive")
    if risk.max_gross_exposure_pct < risk.max_long_exposure_pct:
        raise ConfigError(
            "risk.max_gross_exposure_pct must be >= risk.max_long_exposure_pct"
        )
    if risk.max_gross_exposure_pct + 1e-9 < (
        risk.max_long_exposure_pct + risk.max_short_exposure_pct
    ):
        raise ConfigError(
            "risk.max_gross_exposure_pct must cover max_long_exposure_pct + "
            "max_short_exposure_pct"
        )
    if risk.max_stop_distance_pct < risk.stop_loss_pct:
        raise ConfigError(
            "risk.max_stop_distance_pct must be >= risk.stop_loss_pct "
            f"({risk.max_stop_distance_pct} < {risk.stop_loss_pct})"
        )
    if risk.peak_drawdown_halt_pct <= risk.monthly_kill_switch_pct:
        raise ConfigError(
            "risk.peak_drawdown_halt_pct must exceed risk.monthly_kill_switch_pct — "
            "the absolute halt has to sit below the monthly flatten, or it can never fire"
        )

    e = _require(raw, "execution", "")
    order_type = _require(e, "order_type", "execution")
    if order_type != "limit":
        raise ConfigError(f"execution.order_type must be 'limit' (never market), got {order_type!r}")
    execution = ExecutionLimits(
        order_type=order_type,
        max_limit_slippage_pct=_fraction(
            _require(e, "max_limit_slippage_pct", "execution"), "execution.max_limit_slippage_pct"
        ),
        no_entry_first_minutes=int(_require(e, "no_entry_first_minutes", "execution")),
        no_entry_last_minutes=int(_require(e, "no_entry_last_minutes", "execution")),
        market_open=_parse_time(_require(e, "market_open", "execution"), "execution.market_open"),
        market_close=_parse_time(_require(e, "market_close", "execution"), "execution.market_close"),
        timezone=str(_require(e, "timezone", "execution")),
    )
    if execution.market_open >= execution.market_close:
        raise ConfigError("execution.market_open must be before execution.market_close")

    u = _require(raw, "universe", "")
    universe = UniverseFilters(
        min_price=float(_require(u, "min_price", "universe")),
        min_avg_dollar_volume=float(_require(u, "min_avg_dollar_volume", "universe")),
        exclude_ipo_days=int(_require(u, "exclude_ipo_days", "universe")),
        allow_short=bool(_require(u, "allow_short", "universe")),
        inverse_etfs=tuple(u.get("inverse_etfs", [])),
    )
    # Shorting is an explicit opt-in (enabled 2026-07-23 for the Alpaca path,
    # with user authorization). When enabled, a short-exposure cap is REQUIRED —
    # shorts without a gross cap have unbounded loss.
    if universe.allow_short and not (0 < risk.max_short_exposure_pct <= 0.5):
        raise ConfigError(
            "universe.allow_short=true requires risk.max_short_exposure_pct in (0, 0.5]"
        )

    sleeves = _require(raw, "sleeves", "")
    if not isinstance(sleeves, dict) or not sleeves:
        raise ConfigError("sleeves must be a non-empty mapping")

    total_alloc = sum(float(s.get("allocation", 0)) for s in sleeves.values())
    if total_alloc > 1.0 + 1e-9:
        raise ConfigError(f"sleeve allocations sum to {total_alloc:.4f}, which exceeds 1.0")

    leveraged_symbols = frozenset(sleeves.get("leveraged", {}).get("symbols", []))

    paper = raw.get("paper_portfolio", {})
    if paper:
        alloc = paper.get("sleeves", {})
        total = sum(float(v) for v in alloc.values())
        if not alloc or total > 1.0 + 1e-9:
            raise ConfigError(f"paper_portfolio.sleeves must sum to <= 1.0, got {total:.4f}")

    return Config(
        mode=mode,
        starting_equity=float(starting_equity),
        risk=risk,
        execution=execution,
        universe=universe,
        sleeves=sleeves,
        sleeves_paper=paper,
        leveraged_symbols=leveraged_symbols,
    )
