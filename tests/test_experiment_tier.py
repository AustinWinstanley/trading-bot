"""Tests for the experiment-tier framework: the second, lighter-weight
evidence tier for capped, pre-registered paper-trading experiments —
distinct from the hard promotion gate that governs core allocation.

Covers three layers:
  1. engine/config.py's ExperimentConfig schema and validation.
  2. engine/risk.py's gate governance (off/shadow rejection, allocation-cap
     shrink, stand-down rejection, compute_experiment_standdowns).
  3. scripts/run_daily.py's pure aggregation helpers (held-sleeve lookup,
     exposure/unrealized-P&L aggregation) — no experiment is live yet, so
     these are exercised with synthetic data rather than a live run.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import sqlite3
from zoneinfo import ZoneInfo

import pytest
import yaml

from engine.config import (Config, ConfigError, ExperimentConfig,
                           MAX_TOTAL_EXPERIMENT_ALLOCATION_PCT, load_config)
from engine.risk import (AccountState, MarketContext, Position, RiskState,
                         SymbolData, compute_experiment_standdowns, evaluate)
from scripts.run_daily import (experiment_exposure_and_unrealized_pnl,
                               held_sleeve_by_symbol)

ET = ZoneInfo("America/New_York")
EQUITY = 10_000.0

# An existing, already-committed registration document — stands in for a
# real pre-registration file without inventing a new fixture on disk.
EXISTING_REGISTRATION = "reports/bull_put_delta_selected_shadow_launch.json"


# --------------------------------------------------------------------------
# Layer 1: config schema / validation
# --------------------------------------------------------------------------


def _base_raw_config() -> dict:
    """A minimal but fully valid config dict, mirroring config.yaml's shape,
    so each test only needs to vary the `experiments:` block."""
    return {
        "mode": "paper",
        "account": {"starting_equity": 10000.0},
        "risk": {
            "max_position_pct": 0.15,
            "max_leveraged_exposure_pct": 0.2,
            "max_long_exposure_pct": 1.0,
            "max_gross_exposure_pct": 1.0,
            "max_positions": 60,
            "stop_loss_pct": 0.08,
            "stop_atr_multiple": 2.0,
            "max_stop_distance_pct": 0.15,
            "daily_loss_limit_pct": 0.04,
            "monthly_kill_switch_pct": 0.1,
            "peak_drawdown_halt_pct": 0.2,
            "allow_averaging_down": False,
            "loss_reentry_block_days": 5,
        },
        "execution": {
            "order_type": "limit",
            "max_limit_slippage_pct": 0.003,
            "no_entry_first_minutes": 10,
            "no_entry_last_minutes": 10,
            "market_open": "09:30",
            "market_close": "16:00",
            "timezone": "America/New_York",
        },
        "universe": {
            "min_price": 5.0,
            "min_avg_dollar_volume": 3_000_000,
            "exclude_ipo_days": 180,
            "allow_short": False,
        },
        "sleeves": {"momentum": {"allocation": 1.0}},
    }


def _load(tmp_path, raw: dict, **kwargs) -> Config:
    path = tmp_path / "test_config.yaml"
    path.write_text(yaml.dump(raw))
    return load_config(path, **kwargs)


def test_no_experiments_block_is_empty_dict(tmp_path):
    cfg = _load(tmp_path, _base_raw_config())
    assert cfg.experiments == {}


def test_valid_experiment_loads(tmp_path):
    raw = _base_raw_config()
    raw["experiments"] = {
        "bull_put_live": {
            "status": "paper",
            "allocation_pct": 0.05,
            "max_cumulative_loss_pct": 0.02,
            "registration": EXISTING_REGISTRATION,
        }
    }
    cfg = _load(tmp_path, raw)
    assert set(cfg.experiments) == {"bull_put_live"}
    exp = cfg.experiments["bull_put_live"]
    assert exp.status == "paper"
    assert exp.allocation_pct == 0.05
    assert exp.is_paper_active


def test_off_experiment_does_not_require_registration_file(tmp_path):
    raw = _base_raw_config()
    raw["experiments"] = {
        "not_yet_designed": {
            "status": "off",
            "allocation_pct": 0.05,
            "max_cumulative_loss_pct": 0.02,
            "registration": "reports/experiments/does_not_exist_yet.json",
        }
    }
    cfg = _load(tmp_path, raw)
    assert cfg.experiments["not_yet_designed"].status == "off"


def test_non_off_experiment_requires_registration_file_to_exist(tmp_path):
    raw = _base_raw_config()
    raw["experiments"] = {
        "no_registration": {
            "status": "shadow",
            "allocation_pct": 0.05,
            "max_cumulative_loss_pct": 0.02,
            "registration": "reports/experiments/does_not_exist_yet.json",
        }
    }
    with pytest.raises(ConfigError, match="registration file"):
        _load(tmp_path, raw)


def test_validate_experiments_false_skips_only_the_registration_file_check(tmp_path):
    """Reproduces a 2026-08-13 production incident: the dashboard container
    doesn't mount reports/, so load_config()'s default registration-file
    check always fails there even when the file genuinely exists on the
    host — which took down /summary, /positions, and /equity-curve with a
    500 for any profile with a non-off experiment. validate_experiments=False
    is the dashboard's opt-out; every other rule must still apply."""
    raw = _base_raw_config()
    raw["experiments"] = {
        "no_registration": {
            "status": "paper",
            "allocation_pct": 0.05,
            "max_cumulative_loss_pct": 0.02,
            "registration": "reports/experiments/does_not_exist_yet.json",
        }
    }
    cfg = _load(tmp_path, raw, validate_experiments=False)
    assert cfg.experiments["no_registration"].status == "paper"

    # Still enforced: a structurally invalid block is not waved through.
    raw["experiments"]["no_registration"]["max_cumulative_loss_pct"] = 0.5  # > allocation_pct
    with pytest.raises(ConfigError, match="max_cumulative_loss_pct"):
        _load(tmp_path, raw, validate_experiments=False)


def test_invalid_status_rejected(tmp_path):
    raw = _base_raw_config()
    raw["experiments"] = {
        "bad": {
            "status": "active",  # not a recognized status
            "allocation_pct": 0.05,
            "max_cumulative_loss_pct": 0.02,
            "registration": EXISTING_REGISTRATION,
        }
    }
    with pytest.raises(ConfigError, match="status"):
        _load(tmp_path, raw)


def test_max_cumulative_loss_pct_cannot_exceed_allocation_pct(tmp_path):
    raw = _base_raw_config()
    raw["experiments"] = {
        "over_budget": {
            "status": "shadow",
            "allocation_pct": 0.02,
            "max_cumulative_loss_pct": 0.05,  # > allocation_pct
            "registration": EXISTING_REGISTRATION,
        }
    }
    with pytest.raises(ConfigError, match="max_cumulative_loss_pct"):
        _load(tmp_path, raw)


def test_total_active_allocation_cap_enforced(tmp_path):
    raw = _base_raw_config()
    raw["experiments"] = {
        "a": {"status": "paper", "allocation_pct": 0.2,
              "max_cumulative_loss_pct": 0.05, "registration": EXISTING_REGISTRATION},
        "b": {"status": "shadow", "allocation_pct": 0.15,
              "max_cumulative_loss_pct": 0.05, "registration": EXISTING_REGISTRATION},
    }
    # 0.2 + 0.15 = 0.35 > MAX_TOTAL_EXPERIMENT_ALLOCATION_PCT (0.30)
    assert 0.2 + 0.15 > MAX_TOTAL_EXPERIMENT_ALLOCATION_PCT
    with pytest.raises(ConfigError, match="exceeds"):
        _load(tmp_path, raw)


def test_off_experiments_excluded_from_allocation_cap(tmp_path):
    raw = _base_raw_config()
    raw["experiments"] = {
        "a": {"status": "paper", "allocation_pct": 0.2,
              "max_cumulative_loss_pct": 0.05, "registration": EXISTING_REGISTRATION},
        # off, so its allocation_pct is not counted even though 0.2+0.5 > cap
        "b": {"status": "off", "allocation_pct": 0.5,
              "max_cumulative_loss_pct": 0.05,
              "registration": "reports/experiments/does_not_exist_yet.json"},
    }
    cfg = _load(tmp_path, raw)
    assert set(cfg.experiments) == {"a", "b"}


def test_experiments_block_must_be_a_mapping(tmp_path):
    raw = _base_raw_config()
    raw["experiments"] = ["not", "a", "mapping"]
    with pytest.raises(ConfigError, match="mapping"):
        _load(tmp_path, raw)


# --------------------------------------------------------------------------
# Layer 2: gate governance
# --------------------------------------------------------------------------


@pytest.fixture
def cfg_with_experiment() -> Config:
    base = load_config()
    experiment = ExperimentConfig(
        name="bull_put_live", status="paper", allocation_pct=0.05,
        max_cumulative_loss_pct=0.02, registration_path=EXISTING_REGISTRATION,
    )
    return dataclasses.replace(base, experiments={"bull_put_live": experiment})


@pytest.fixture
def ctx() -> MarketContext:
    return MarketContext(
        now=dt.datetime(2026, 8, 12, 12, 30, tzinfo=ET),
        is_trading_day=True,
        symbols={
            "SPY": SymbolData(price=500.0, atr14=5.0, avg_dollar_volume_20d=5e9, shortable=True),
            "CHEAP": SymbolData(price=50.0, atr14=1.0, avg_dollar_volume_20d=1e9, shortable=True),
        },
    )


@pytest.fixture
def clean_risk() -> RiskState:
    return RiskState(peak_equity=EQUITY, day_start_equity=EQUITY, month_start_equity=EQUITY)


def buy(symbol="SPY", notional=500.0, limit=500.0, sleeve="bull_put_live"):
    return {"symbol": symbol, "side": "buy", "sleeve": sleeve,
            "notional": notional, "limit_price": limit, "rationale": "test"}


def short(symbol="SPY", notional=500.0, limit=500.0, sleeve="bull_put_live"):
    return {"symbol": symbol, "side": "short", "sleeve": sleeve,
            "notional": notional, "limit_price": limit, "rationale": "test"}


def test_paper_status_experiment_buy_is_approved_within_cap(cfg_with_experiment, ctx, clean_risk):
    account = AccountState(equity=EQUITY, cash=EQUITY, positions={})
    result = evaluate([buy(notional=200.0)], account, clean_risk, ctx, cfg_with_experiment)
    assert len(result.approved) == 1
    assert result.approved[0].notional == 200.0


def test_shadow_status_experiment_never_places_a_real_order(cfg_with_experiment, ctx, clean_risk):
    experiment = dataclasses.replace(
        cfg_with_experiment.experiments["bull_put_live"], status="shadow"
    )
    cfg = dataclasses.replace(cfg_with_experiment, experiments={"bull_put_live": experiment})
    account = AccountState(equity=EQUITY, cash=EQUITY, positions={})
    for proposal in (buy(), short()):
        result = evaluate([proposal], account, clean_risk, ctx, cfg)
        assert len(result.approved) == 0
        assert "shadow-only" in result.rejected[0].reason


def test_off_status_experiment_rejects_new_entries(cfg_with_experiment, ctx, clean_risk):
    experiment = dataclasses.replace(
        cfg_with_experiment.experiments["bull_put_live"], status="off"
    )
    cfg = dataclasses.replace(cfg_with_experiment, experiments={"bull_put_live": experiment})
    account = AccountState(equity=EQUITY, cash=EQUITY, positions={})
    for proposal in (buy(), short()):
        result = evaluate([proposal], account, clean_risk, ctx, cfg)
        assert len(result.approved) == 0
        assert "is off" in result.rejected[0].reason


def test_stood_down_experiment_rejects_new_entries(cfg_with_experiment, ctx, clean_risk):
    account = AccountState(equity=EQUITY, cash=EQUITY, positions={})
    risk_state = dataclasses.replace(
        clean_risk, experiment_standdowns=frozenset({"bull_put_live"})
    )
    for proposal in (buy(), short()):
        result = evaluate([proposal], account, risk_state, ctx, cfg_with_experiment)
        assert len(result.approved) == 0
        assert "stood down" in result.rejected[0].reason


def test_standdown_does_not_block_exits(cfg_with_experiment, ctx, clean_risk):
    """Sells/covers must always clear a stood-down sleeve — the flatten
    path depends on this staying true."""
    position = Position(symbol="SPY", qty=1.0, avg_entry_price=500.0, current_price=500.0)
    account = AccountState(equity=EQUITY, cash=EQUITY, positions={"SPY": position})
    risk_state = dataclasses.replace(
        clean_risk, experiment_standdowns=frozenset({"bull_put_live"})
    )
    proposal = {"symbol": "SPY", "side": "sell", "sleeve": "bull_put_live",
                "notional": 500.0, "limit_price": 500.0, "rationale": "flatten"}
    result = evaluate([proposal], account, risk_state, ctx, cfg_with_experiment)
    assert len(result.approved) == 1
    assert result.approved[0].side == "sell"


def test_buy_shrinks_to_allocation_cap(cfg_with_experiment, ctx, clean_risk):
    # allocation_pct=0.05 * equity 10_000 = $500 cap; $300 already exposed.
    account = AccountState(equity=EQUITY, cash=EQUITY, positions={},
                           experiment_gross_exposure={"bull_put_live": 300.0})
    result = evaluate([buy(notional=500.0)], account, clean_risk, ctx, cfg_with_experiment)
    assert len(result.approved) == 1
    assert result.approved[0].notional == pytest.approx(200.0)
    assert any("allocation cap" in a for a in result.approved[0].adjustments)


def test_buy_rejected_when_allocation_cap_already_full(cfg_with_experiment, ctx, clean_risk):
    account = AccountState(equity=EQUITY, cash=EQUITY, positions={},
                           experiment_gross_exposure={"bull_put_live": 500.0})
    result = evaluate([buy(notional=200.0)], account, clean_risk, ctx, cfg_with_experiment)
    assert len(result.approved) == 0
    assert "allocation cap" in result.rejected[0].reason


def test_short_shrinks_to_allocation_cap(cfg_with_experiment, ctx, clean_risk):
    account = AccountState(equity=EQUITY, cash=EQUITY, positions={},
                           shorting_enabled=True,
                           experiment_gross_exposure={"bull_put_live": 350.0})
    cfg = dataclasses.replace(
        cfg_with_experiment,
        universe=dataclasses.replace(cfg_with_experiment.universe, allow_short=True),
    )
    result = evaluate(
        [short(symbol="CHEAP", notional=500.0, limit=50.0)], account, clean_risk, ctx, cfg
    )
    assert len(result.approved) == 1
    # Short-path adjustment messages are generic (pre-existing behaviour —
    # they don't distinguish which cap bound); the shrunk notional is what
    # actually proves the experiment allocation cap, not short/gross, bound.
    assert result.approved[0].notional == pytest.approx(150.0)
    assert result.approved[0].was_shrunk


def test_two_proposals_share_the_same_allocation_room(cfg_with_experiment, ctx, clean_risk):
    """committed_experiment must accumulate across proposals in one run —
    two $300 buys against a $500 cap must not both clear in full."""
    account = AccountState(equity=EQUITY, cash=EQUITY, positions={})
    proposals = [buy(notional=300.0), buy(notional=300.0)]
    result = evaluate(proposals, account, clean_risk, ctx, cfg_with_experiment)
    total_approved = sum(o.notional for o in result.approved)
    assert total_approved <= 500.0 + 1e-6
    assert len(result.approved) == 2  # second is shrunk, not rejected outright
    assert result.approved[1].notional == pytest.approx(200.0)


def test_experiment_invariant_catches_shadow_status_bypassing_inline_check(
    cfg_with_experiment, ctx, clean_risk, monkeypatch
):
    """If a future code path skipped the inline shadow/off rejection, the
    runtime invariant must still catch it before an order could reach the
    broker — the same defense-in-depth pattern as the stop-exempt check."""
    import engine.risk as risk_mod

    account = AccountState(equity=EQUITY, cash=EQUITY, positions={})
    result = risk_mod.GateResult(approved=[
        risk_mod.ApprovedOrder(
            symbol="SPY", side="buy", sleeve="bull_put_live", notional=100.0,
            qty=0.2, limit_price=500.0, stop_price=490.0, requested_notional=100.0,
        )
    ])
    shadow_cfg = dataclasses.replace(
        cfg_with_experiment,
        experiments={"bull_put_live": dataclasses.replace(
            cfg_with_experiment.experiments["bull_put_live"], status="shadow"
        )},
    )
    with pytest.raises(AssertionError, match="not 'paper'"):
        risk_mod._assert_gate_invariants(result, shadow_cfg, clean_risk)


# --------------------------------------------------------------------------
# compute_experiment_standdowns (pure)
# --------------------------------------------------------------------------


def test_compute_standdowns_flags_breach():
    experiment = ExperimentConfig(
        name="bull_put_live", status="paper", allocation_pct=0.05,
        max_cumulative_loss_pct=0.02, registration_path=EXISTING_REGISTRATION,
    )
    cfg = dataclasses.replace(load_config(), experiments={"bull_put_live": experiment})
    standdowns = compute_experiment_standdowns(cfg, {"bull_put_live": -0.025})
    assert standdowns == frozenset({"bull_put_live"})


def test_compute_standdowns_exact_threshold_breaches():
    experiment = ExperimentConfig(
        name="x", status="shadow", allocation_pct=0.05,
        max_cumulative_loss_pct=0.02, registration_path=EXISTING_REGISTRATION,
    )
    cfg = dataclasses.replace(load_config(), experiments={"x": experiment})
    assert compute_experiment_standdowns(cfg, {"x": -0.02}) == frozenset({"x"})


def test_compute_standdowns_ignores_gains_and_small_losses():
    experiment = ExperimentConfig(
        name="x", status="paper", allocation_pct=0.05,
        max_cumulative_loss_pct=0.02, registration_path=EXISTING_REGISTRATION,
    )
    cfg = dataclasses.replace(load_config(), experiments={"x": experiment})
    assert compute_experiment_standdowns(cfg, {"x": 0.10}) == frozenset()
    assert compute_experiment_standdowns(cfg, {"x": -0.01}) == frozenset()
    assert compute_experiment_standdowns(cfg, {}) == frozenset()


def test_compute_standdowns_excludes_off_experiments():
    experiment = ExperimentConfig(
        name="x", status="off", allocation_pct=0.05,
        max_cumulative_loss_pct=0.02,
        registration_path="reports/experiments/does_not_exist_yet.json",
    )
    cfg = dataclasses.replace(load_config(), experiments={"x": experiment})
    # Even a catastrophic recorded loss cannot stand down an off experiment.
    assert compute_experiment_standdowns(cfg, {"x": -0.9}) == frozenset()


# --------------------------------------------------------------------------
# Layer 3: run_daily.py pure aggregation helpers
# --------------------------------------------------------------------------


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    connection.execute("""
        CREATE TABLE orders(
            ts TEXT, symbol TEXT, side TEXT, sleeve TEXT, qty REAL, notional REAL)
    """)
    yield connection
    connection.close()


def _insert_order(conn, ts, symbol, side, sleeve):
    conn.execute(
        "INSERT INTO orders(ts, symbol, side, sleeve, qty, notional) VALUES (?,?,?,?,?,?)",
        (ts, symbol, side, sleeve, 1.0, 100.0),
    )


def test_held_sleeve_by_symbol_picks_most_recent_entry_order(conn):
    _insert_order(conn, "2026-08-01T10:00:00", "SPY", "buy", "equity_core")
    _insert_order(conn, "2026-08-10T10:00:00", "SPY", "buy", "bull_put_live")
    _insert_order(conn, "2026-08-11T10:00:00", "SPY", "sell", "stop")  # exits don't count
    result = held_sleeve_by_symbol(conn, ["SPY"])
    assert result == {"SPY": "bull_put_live"}


def test_held_sleeve_by_symbol_omits_symbol_with_no_entry_order(conn):
    assert held_sleeve_by_symbol(conn, ["NEVER_TRADED"]) == {}


def test_experiment_exposure_and_unrealized_pnl_aggregates_by_experiment():
    experiment = ExperimentConfig(
        name="bull_put_live", status="paper", allocation_pct=0.05,
        max_cumulative_loss_pct=0.02, registration_path=EXISTING_REGISTRATION,
    )
    cfg = dataclasses.replace(load_config(), experiments={"bull_put_live": experiment})
    positions = {
        "SPY": Position(symbol="SPY", qty=1.0, avg_entry_price=500.0, current_price=550.0),
        "QQQ": Position(symbol="QQQ", qty=1.0, avg_entry_price=400.0, current_price=400.0),
    }
    held_sleeve = {"SPY": "bull_put_live", "QQQ": "unrelated_sleeve"}
    exposure, unrealized = experiment_exposure_and_unrealized_pnl(positions, held_sleeve, cfg)
    assert exposure == {"bull_put_live": 550.0}
    # unrealized_pct (10%) applied to current market value ($550), matching
    # the same approximation used for realized P&L in run_daily.py's main().
    assert unrealized["bull_put_live"] == pytest.approx(55.0)


def test_experiment_exposure_and_unrealized_pnl_empty_when_no_experiments_configured():
    cfg = load_config()  # no experiments
    positions = {"SPY": Position(symbol="SPY", qty=1.0, avg_entry_price=500.0, current_price=550.0)}
    exposure, unrealized = experiment_exposure_and_unrealized_pnl(
        positions, {"SPY": "bull_put_live"}, cfg
    )
    assert exposure == {}
    assert unrealized == {}
