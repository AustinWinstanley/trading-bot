"""Schema-validated config loader.

An invalid config raises. It never falls back to defaults — a typo in a risk
limit must stop the bot, not silently loosen it.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = REPO_ROOT / "config.yaml"

VALID_MODES = {"paper", "live", "halt"}
# off = no proposals ever generated; shadow = read-only observation, may
# never place a real order; paper = real (paper-broker) orders, sized and
# risk-bounded by the gate. See engine/risk.py's experiment governance.
VALID_EXPERIMENT_STATUSES = {"off", "shadow", "paper"}
# Core validated allocation must always keep the majority of lab equity —
# the experiment tier is capped bandwidth for unproven ideas, not a second
# core. Sum of allocation_pct across every non-off experiment is checked
# against this at config-load time.
MAX_TOTAL_EXPERIMENT_ALLOCATION_PCT = 0.30


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
    # Sleeves that manage risk by rebalancing rather than per-position exits.
    # See reports/risk_overlay_study.json — on MOM_LS the stop cost CAGR and
    # Sharpe in both windows and both profiles, and the re-entry block is
    # inert without it. Empty means every sleeve is stopped, as before.
    stop_exempt_sleeves: frozenset[str] = frozenset()
    reentry_block_exempt_sleeves: frozenset[str] = frozenset()
    # A diversified-index sleeve combination (e.g. equity_core + trend, both
    # SPY) can have a combined target above max_position_pct even though it
    # carries none of the single-name risk that cap exists to bound. Without
    # this, max_position_pct silently strangles the sleeve's target below
    # what portfolio.py builds — confirmed live: SPY was rejected at the base
    # cap on 19 of the last ~19 rebalance opportunities. `None` (the default)
    # disables this entirely, matching pre-existing behaviour.
    elevated_position_pct: float | None = None
    elevated_position_sleeves: frozenset[str] = frozenset()

    def stops_apply_to(self, sleeve: str) -> bool:
        return sleeve not in self.stop_exempt_sleeves

    def reentry_block_applies_to(self, sleeve: str) -> bool:
        return sleeve not in self.reentry_block_exempt_sleeves

    def position_cap_pct(self, sleeve: str) -> float:
        """Max single-position fraction of equity for this proposal's sleeve.

        A symbol's sleeve attribution can be a `+`-joined combination of
        origins (`portfolio.py` builds e.g. `"equity_core+trend"` for SPY
        when both sleeves hold it). The elevated cap applies only when
        *every* contributing sleeve qualifies — a name that is part
        index-core and part something else does not inherit the wider cap
        through a substring match.
        """
        if self.elevated_position_pct is None or not self.elevated_position_sleeves:
            return self.max_position_pct
        parts = {p for p in sleeve.split("+") if p}
        if parts and parts <= self.elevated_position_sleeves:
            return self.elevated_position_pct
        return self.max_position_pct


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
class ExperimentConfig:
    """One pre-registered, capped experiment-tier sleeve.

    Distinct from the hard promotion gate (backtest/promotion.py) that
    governs core allocation: this is the lighter-weight second tier for
    capped, pre-registered paper-trading experiments, evaluated on
    plausible evidence rather than the full frozen-window study. Promotion
    from here to core allocation still requires the hard gate — this
    dataclass carries no promotion authority of its own.
    """

    name: str
    status: str
    allocation_pct: float
    max_cumulative_loss_pct: float
    registration_path: str

    @property
    def is_paper_active(self) -> bool:
        return self.status == "paper"


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
    experiments: dict[str, ExperimentConfig] = field(default_factory=dict)


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


def _sleeve_set(value, name: str) -> frozenset[str]:
    """Parse an optional list of sleeve names into a frozenset.

    Absent means empty, i.e. the control applies everywhere — the pre-existing
    behaviour. A typo must not silently disable a risk control, so anything
    that is not a list of non-empty strings is a hard config error.
    """
    if value is None:
        return frozenset()
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise ConfigError(f"{name} must be a list of sleeve names, got {value!r}")
    names = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ConfigError(f"{name} entries must be non-empty strings, got {item!r}")
        names.append(item.strip())
    return frozenset(names)


def _positive_number(value, name: str, *, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{name} must be a number, got {value!r}")
    value = float(value)
    if not (0 < value <= high):
        raise ConfigError(f"{name} must be in (0, {high}], got {value}")
    return value


def _parse_experiments(raw: dict) -> dict[str, ExperimentConfig]:
    """Parse the top-level `experiments:` block (kept separate from the
    existing `paper_portfolio.options_experiments` block deliberately — that
    one is a read-only quote-collection convention that already works;
    this is the newer, stricter, real-order-capable tier).

    A registration document (the pre-committed hypothesis, measurement plan,
    and review bar) is required to exist on disk before `status` may leave
    "off". This is enforced here, at load time, so an experiment can never
    silently start sizing real activity against a promise nobody wrote down.
    """
    block = raw.get("experiments")
    if block is None:
        return {}
    if not isinstance(block, dict):
        raise ConfigError("experiments must be a mapping of name -> settings")

    experiments: dict[str, ExperimentConfig] = {}
    total_active_alloc = 0.0
    for name, settings in block.items():
        if not isinstance(name, str) or not name.strip():
            raise ConfigError(f"experiments key must be a non-empty string, got {name!r}")
        if not isinstance(settings, dict):
            raise ConfigError(f"experiments.{name} must be a mapping")

        status = _require(settings, "status", f"experiments.{name}")
        if status not in VALID_EXPERIMENT_STATUSES:
            raise ConfigError(
                f"experiments.{name}.status must be one of "
                f"{sorted(VALID_EXPERIMENT_STATUSES)}, got {status!r}"
            )
        allocation_pct = _fraction(
            _require(settings, "allocation_pct", f"experiments.{name}"),
            f"experiments.{name}.allocation_pct",
        )
        max_cumulative_loss_pct = _fraction(
            _require(settings, "max_cumulative_loss_pct", f"experiments.{name}"),
            f"experiments.{name}.max_cumulative_loss_pct",
        )
        registration_path = _require(settings, "registration", f"experiments.{name}")
        if not isinstance(registration_path, str) or not registration_path.strip():
            raise ConfigError(f"experiments.{name}.registration must be a non-empty path string")
        registration_path = registration_path.strip()

        if status != "off" and not (REPO_ROOT / registration_path).exists():
            raise ConfigError(
                f"experiments.{name}.status is {status!r} but its registration file "
                f"{registration_path!r} does not exist — write the pre-registration "
                "(hypothesis, measurement plan, review bar) before turning this on"
            )
        if max_cumulative_loss_pct > allocation_pct:
            raise ConfigError(
                f"experiments.{name}.max_cumulative_loss_pct ({max_cumulative_loss_pct}) "
                f"must be <= allocation_pct ({allocation_pct}) — a sleeve cannot be "
                "permitted to lose more of equity than it is allocated"
            )

        experiments[name] = ExperimentConfig(
            name=name,
            status=status,
            allocation_pct=allocation_pct,
            max_cumulative_loss_pct=max_cumulative_loss_pct,
            registration_path=registration_path,
        )
        if status != "off":
            total_active_alloc += allocation_pct

    if total_active_alloc > MAX_TOTAL_EXPERIMENT_ALLOCATION_PCT + 1e-9:
        raise ConfigError(
            f"experiments: total allocation_pct across non-off experiments is "
            f"{total_active_alloc:.2%}, which exceeds the "
            f"{MAX_TOTAL_EXPERIMENT_ALLOCATION_PCT:.0%} cap — core allocation must "
            "keep at least "
            f"{1 - MAX_TOTAL_EXPERIMENT_ALLOCATION_PCT:.0%} of equity"
        )
    return experiments


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
        stop_exempt_sleeves=_sleeve_set(r.get("stop_exempt_sleeves"), "risk.stop_exempt_sleeves"),
        reentry_block_exempt_sleeves=_sleeve_set(
            r.get("reentry_block_exempt_sleeves"), "risk.reentry_block_exempt_sleeves"
        ),
        elevated_position_pct=(
            _fraction(r["elevated_position_pct"], "risk.elevated_position_pct", high=3.0)
            if r.get("elevated_position_pct") is not None
            else None
        ),
        elevated_position_sleeves=_sleeve_set(
            r.get("elevated_position_sleeves"), "risk.elevated_position_sleeves"
        ),
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
    if bool(risk.elevated_position_sleeves) != (risk.elevated_position_pct is not None):
        raise ConfigError(
            "risk.elevated_position_pct and risk.elevated_position_sleeves must be set "
            "together — one without the other is a no-op that reads as configured"
        )
    if (
        risk.elevated_position_pct is not None
        and risk.elevated_position_pct < risk.max_position_pct
    ):
        raise ConfigError(
            "risk.elevated_position_pct must be >= risk.max_position_pct — it is meant "
            "to widen the cap for the listed sleeves, not narrow it"
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
        overlay = paper.get("volatility_overlay", {})
        if overlay:
            overlay_mode = overlay.get("mode", "off")
            if overlay_mode not in {"off", "shadow", "active"}:
                raise ConfigError(
                    "paper_portfolio.volatility_overlay.mode must be 'off' "
                    "'shadow', or 'active'"
                )
            _fraction(
                _require(overlay, "target_vol", "paper_portfolio.volatility_overlay"),
                "paper_portfolio.volatility_overlay.target_vol",
            )
            lookback = _positive_int(
                _require(overlay, "lookback_days", "paper_portfolio.volatility_overlay"),
                "paper_portfolio.volatility_overlay.lookback_days",
            )
            min_observations = _positive_int(
                _require(
                    overlay,
                    "min_observations",
                    "paper_portfolio.volatility_overlay",
                ),
                "paper_portfolio.volatility_overlay.min_observations",
            )
            if min_observations > lookback:
                raise ConfigError(
                    "paper_portfolio.volatility_overlay.min_observations "
                    "must be <= lookback_days"
                )
            _fraction(
                _require(overlay, "min_scale", "paper_portfolio.volatility_overlay"),
                "paper_portfolio.volatility_overlay.min_scale",
            )
        mom_ls_floor = alloc.get("mom_ls")
        if mom_ls_floor and "mom_ls_min_dollar_volume" in paper:
            mom_ls_dv = float(paper["mom_ls_min_dollar_volume"])
            if mom_ls_dv < universe.min_avg_dollar_volume:
                raise ConfigError(
                    "paper_portfolio.mom_ls_min_dollar_volume "
                    f"({mom_ls_dv:,.0f}) must be >= universe.min_avg_dollar_volume "
                    f"({universe.min_avg_dollar_volume:,.0f}) — selection is meant to be "
                    "at least as strict as the daily risk gate, or the gate silently "
                    "becomes the real filter and the weekly one is decorative"
                )

    experiments = _parse_experiments(raw)

    return Config(
        mode=mode,
        starting_equity=float(starting_equity),
        risk=risk,
        execution=execution,
        universe=universe,
        sleeves=sleeves,
        sleeves_paper=paper,
        leveraged_symbols=leveraged_symbols,
        experiments=experiments,
    )
