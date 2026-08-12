"""Adversarial tests for the risk gate.

These are the most important tests in the project. Every test here feeds the
gate a proposal a buggy or misbehaving LLM might realistically produce, and
asserts the gate refuses it or cuts it down to size.

If you change engine/risk.py, this suite is the gate on that change.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from engine.config import Config, ConfigError, load_config
from engine.risk import (
    AccountState,
    MarketContext,
    Position,
    RiskState,
    SymbolData,
    evaluate,
    stop_distance_pct,
)

ET = ZoneInfo("America/New_York")
EQUITY = 4600.0


@pytest.fixture
def cfg() -> Config:
    return load_config()


@pytest.fixture
def ctx() -> MarketContext:
    """Midday on a normal trading day, with a clean liquid universe."""
    return MarketContext(
        now=dt.datetime(2026, 7, 22, 12, 30, tzinfo=ET),
        is_trading_day=True,
        symbols={
            "XLK": SymbolData(price=100.0, atr14=2.0, avg_dollar_volume_20d=500e6),
            "QQQ": SymbolData(price=400.0, atr14=6.0, avg_dollar_volume_20d=2e9),
            "TQQQ": SymbolData(price=50.0, atr14=2.5, avg_dollar_volume_20d=1e9, is_leveraged=True),
            "SOXL": SymbolData(price=20.0, atr14=1.5, avg_dollar_volume_20d=800e6, is_leveraged=True),
            # Deliberate traps, mirroring the user's current holdings.
            "PENNY": SymbolData(price=2.50, atr14=0.3, avg_dollar_volume_20d=100e6),
            "THIN": SymbolData(price=50.0, atr14=1.0, avg_dollar_volume_20d=1e6),
            "NEWIPO": SymbolData(price=50.0, atr14=1.0, avg_dollar_volume_20d=500e6, listed_days=30),
        },
    )


@pytest.fixture
def account() -> AccountState:
    return AccountState(equity=EQUITY, cash=EQUITY, positions={})


@pytest.fixture
def clean_risk() -> RiskState:
    return RiskState(peak_equity=EQUITY, day_start_equity=EQUITY, month_start_equity=EQUITY)


def buy(symbol="XLK", notional=500.0, limit=100.0, sleeve="momentum"):
    return {
        "symbol": symbol,
        "side": "buy",
        "sleeve": sleeve,
        "notional": notional,
        "limit_price": limit,
        "rationale": "test",
    }


def only_rejection(result):
    assert len(result.approved) == 0, f"expected rejection, got approval: {result.approved}"
    assert len(result.rejected) == 1
    return result.rejected[0].reason


# --------------------------------------------------------------------------
# The core contract
# --------------------------------------------------------------------------


def test_gate_never_enlarges_an_order(cfg, account, clean_risk, ctx):
    """The single most important property: approved <= requested, always."""
    proposals = [
        buy("XLK", notional=n)
        for n in (1.0, 50.0, 500.0, 690.0, 5_000.0, 1e6)
    ]
    result = evaluate(proposals, account, clean_risk, ctx, cfg)
    for order in result.approved:
        assert order.notional <= order.requested_notional + 1e-6


def test_rounding_never_enlarges_an_order(cfg, account, clean_risk, ctx):
    """Regression: round() on the approved notional could round *up*.

    Caught by the runtime invariant on the first backtest against real data.
    Any cent-level enlargement is still an enlargement, so the gate floors.
    """
    awkward = [
        buy("XLK", notional=n, limit=p)
        for n, p in [
            (515.4363427315984, 100.0),
            (333.3333333333, 77.77),
            (0.015, 100.0),
            (99.999999, 33.33),
            (690.0, 3.33),
        ]
    ]
    result = evaluate(awkward, account, clean_risk, ctx, cfg)
    for order in result.approved:
        assert order.notional <= order.requested_notional, (
            f"{order.symbol}: approved {order.notional!r} > requested {order.requested_notional!r}"
        )


def test_every_approved_buy_carries_a_stop_below_entry(cfg, account, clean_risk, ctx):
    result = evaluate([buy("XLK"), buy("QQQ", limit=400.0)], account, clean_risk, ctx, cfg)
    assert len(result.approved) == 2
    for order in result.approved:
        assert order.stop_price > 0
        assert order.stop_price < order.limit_price


def test_oversized_position_is_shrunk_not_rejected(cfg, account, clean_risk, ctx):
    """A 50%-of-equity request comes back at exactly the 15% cap."""
    result = evaluate([buy("XLK", notional=2300.0)], account, clean_risk, ctx, cfg)
    assert len(result.approved) == 1
    order = result.approved[0]
    cap = cfg.risk.max_position_pct * EQUITY
    assert order.notional <= cap + 0.01
    assert order.was_shrunk
    assert "max_position_pct" in " ".join(order.adjustments)


def test_elevated_cap_sleeve_reaches_its_wider_target(cfg, account, clean_risk, ctx):
    """SPY (equity_core+trend) can exceed the base 15% cap up to the elevated one."""
    result = evaluate(
        [buy("XLK", notional=4000.0, sleeve="equity_core+trend")], account, clean_risk, ctx, cfg
    )
    assert len(result.approved) == 1
    order = result.approved[0]
    assert cfg.risk.max_position_pct * EQUITY < order.notional
    assert order.notional <= cfg.risk.elevated_position_pct * EQUITY + 0.01


def test_non_elevated_sleeve_still_capped_at_the_base_limit(cfg, account, clean_risk, ctx):
    """MOM_LS keeps the narrow cap — the wider one doesn't leak to other sleeves."""
    result = evaluate([buy("XLK", notional=4000.0, sleeve="mom_ls")], account, clean_risk, ctx, cfg)
    assert len(result.approved) == 1
    order = result.approved[0]
    cap = cfg.risk.max_position_pct * EQUITY
    assert order.notional <= cap + 0.01
    assert order.was_shrunk


def test_partial_overlap_with_an_elevated_sleeve_does_not_widen_the_cap(cfg, account, clean_risk, ctx):
    """A symbol only partly attributed to an elevated sleeve keeps the base cap.

    Sleeve strings can be `+`-joined combinations of origins; the elevated
    cap must require every contributing sleeve to qualify, not just one.
    """
    result = evaluate(
        [buy("XLK", notional=4000.0, sleeve="equity_core+mom_ls")], account, clean_risk, ctx, cfg
    )
    assert len(result.approved) == 1
    order = result.approved[0]
    cap = cfg.risk.max_position_pct * EQUITY
    assert order.notional <= cap + 0.01


def test_repeated_proposals_for_same_symbol_cannot_stack_past_the_cap(cfg, account, clean_risk, ctx):
    """Five separate 15% requests must not add up to 75% of the account."""
    result = evaluate([buy("XLK", notional=690.0) for _ in range(5)], account, clean_risk, ctx, cfg)
    total = sum(o.notional for o in result.approved if o.symbol == "XLK")
    assert total <= cfg.risk.max_position_pct * EQUITY + 0.01


# --------------------------------------------------------------------------
# Malformed input — the LLM wrote garbage
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        "not a list",
        {"unexpected": "shape"},
        42,
    ],
)
def test_malformed_proposal_payload_is_rejected(cfg, account, clean_risk, ctx, bad):
    result = evaluate(bad, account, clean_risk, ctx, cfg)
    assert result.approved == []
    assert len(result.rejected) >= 1


@pytest.mark.parametrize(
    "bad,expect",
    [
        ({}, "symbol"),
        ({"symbol": "XLK"}, "side"),
        ({"symbol": "XLK", "side": "yolo", "sleeve": "x", "notional": 1, "limit_price": 1}, "side"),
        ({"symbol": "XLK", "side": "buy", "sleeve": "m", "notional": -500, "limit_price": 100}, "positive"),
        ({"symbol": "XLK", "side": "buy", "sleeve": "m", "notional": 0, "limit_price": 100}, "positive"),
        ({"symbol": "XLK", "side": "buy", "sleeve": "m", "notional": float("nan"), "limit_price": 100}, "finite"),
        ({"symbol": "XLK", "side": "buy", "sleeve": "m", "notional": float("inf"), "limit_price": 100}, "finite"),
        ({"symbol": "XLK", "side": "buy", "sleeve": "m", "notional": "500", "limit_price": 100}, "number"),
        ({"symbol": "XLK", "side": "buy", "sleeve": "m", "notional": 500, "limit_price": 0}, "positive"),
        ({"symbol": "", "side": "buy", "sleeve": "m", "notional": 500, "limit_price": 100}, "symbol"),
        ({"symbol": "XLK", "side": "buy", "notional": 500, "limit_price": 100}, "sleeve"),
        ("a string where an object should be", "not an object"),
        (None, "not an object"),
    ],
)
def test_malformed_individual_proposals_are_rejected(cfg, account, clean_risk, ctx, bad, expect):
    result = evaluate([bad], account, clean_risk, ctx, cfg)
    assert expect in only_rejection(result).lower() or expect in only_rejection(result)


def test_unknown_symbol_is_rejected_rather_than_assumed(cfg, account, clean_risk, ctx):
    result = evaluate([buy("HALLUCINATED")], account, clean_risk, ctx, cfg)
    assert "no market data" in only_rejection(result)


# --------------------------------------------------------------------------
# Averaging down — never, under any circumstances
# --------------------------------------------------------------------------


def test_averaging_down_is_rejected(cfg, clean_risk, ctx):
    account = AccountState(
        equity=EQUITY,
        cash=2000.0,
        positions={"XLK": Position("XLK", qty=5, avg_entry_price=120.0, current_price=100.0)},
    )
    result = evaluate([buy("XLK", notional=200.0)], account, clean_risk, ctx, cfg)
    assert "averaging down" in only_rejection(result)


def test_averaging_down_is_still_rejected_for_an_unlisted_sleeve_even_with_exemptions_configured(
    cfg, clean_risk, ctx
):
    """The exemption is opt-in per sleeve, not a global switch."""
    exempt_cfg = dataclasses.replace(
        cfg, risk=dataclasses.replace(cfg.risk, restoration_exempt_sleeves=frozenset({"mom_ls"}))
    )
    account = AccountState(
        equity=EQUITY,
        cash=2000.0,
        positions={"XLK": Position("XLK", qty=5, avg_entry_price=120.0, current_price=100.0)},
    )
    result = evaluate(
        [buy("XLK", notional=200.0, sleeve="momentum")], account, clean_risk, ctx, exempt_cfg
    )
    assert "averaging down" in only_rejection(result)


def test_averaging_down_is_permitted_for_a_restoration_exempt_sleeve(cfg, clean_risk, ctx):
    exempt_cfg = dataclasses.replace(
        cfg, risk=dataclasses.replace(cfg.risk, restoration_exempt_sleeves=frozenset({"mom_ls"}))
    )
    account = AccountState(
        equity=EQUITY,
        cash=2000.0,
        positions={"XLK": Position("XLK", qty=5, avg_entry_price=120.0, current_price=100.0)},
    )
    result = evaluate(
        [buy("XLK", notional=200.0, sleeve="mom_ls")], account, clean_risk, ctx, exempt_cfg
    )
    assert len(result.approved) == 1
    assert len(result.rejected) == 0


def test_restoration_exemption_does_not_widen_any_other_cap(cfg, clean_risk, ctx):
    """The exemption removes ONLY the loser check — position/exposure caps
    still bind exactly as they do for a winner."""
    exempt_cfg = dataclasses.replace(
        cfg, risk=dataclasses.replace(cfg.risk, restoration_exempt_sleeves=frozenset({"mom_ls"}))
    )
    account = AccountState(
        equity=EQUITY,
        cash=100_000.0,
        positions={"XLK": Position("XLK", qty=5, avg_entry_price=120.0, current_price=100.0)},
    )
    huge_request = 5_000.0  # far past max_position_pct * EQUITY
    result = evaluate(
        [buy("XLK", notional=huge_request, sleeve="mom_ls")], account, clean_risk, ctx, exempt_cfg
    )
    assert len(result.approved) == 1
    cap = cfg.risk.max_position_pct * EQUITY
    assert result.approved[0].notional < huge_request
    assert result.approved[0].notional <= cap - account.positions["XLK"].market_value + 1e-6


def test_adding_to_a_winner_is_allowed_but_still_capped(cfg, clean_risk, ctx):
    account = AccountState(
        equity=EQUITY,
        cash=2000.0,
        positions={"XLK": Position("XLK", qty=6, avg_entry_price=90.0, current_price=100.0)},
    )
    result = evaluate([buy("XLK", notional=500.0)], account, clean_risk, ctx, cfg)
    assert len(result.approved) == 1
    cap = cfg.risk.max_position_pct * EQUITY
    assert result.approved[0].notional + 600.0 <= cap + 0.01


# --------------------------------------------------------------------------
# Circuit breakers
# --------------------------------------------------------------------------


def test_monthly_kill_switch_blocks_entries_and_flattens(cfg, ctx):
    account = AccountState(equity=4000.0, cash=4000.0, positions={})
    risk = RiskState(peak_equity=EQUITY, day_start_equity=4000.0, month_start_equity=EQUITY)
    result = evaluate([buy("XLK")], account, risk, ctx, cfg)
    assert result.flatten_all
    assert result.new_entries_blocked
    assert result.approved == []


def test_peak_drawdown_halts_everything(cfg, ctx):
    account = AccountState(equity=3700.0, cash=3700.0, positions={})
    risk = RiskState(peak_equity=EQUITY, day_start_equity=3700.0, month_start_equity=3700.0)
    result = evaluate([buy("XLK")], account, risk, ctx, cfg)
    assert result.halt_reason is not None
    assert result.flatten_all
    assert result.approved == []


def test_daily_loss_limit_blocks_new_entries_without_flattening(cfg, ctx):
    account = AccountState(equity=4370.0, cash=4370.0, positions={})
    risk = RiskState(peak_equity=EQUITY, day_start_equity=EQUITY, month_start_equity=EQUITY)
    result = evaluate([buy("XLK")], account, risk, ctx, cfg)
    assert result.new_entries_blocked
    assert not result.flatten_all
    assert result.approved == []


def test_halt_mode_in_config_rejects_everything(account, clean_risk, ctx, tmp_path):
    import yaml

    raw = yaml.safe_load(open("config.yaml"))
    raw["mode"] = "halt"
    p = tmp_path / "halt.yaml"
    p.write_text(yaml.safe_dump(raw))
    halted_cfg = load_config(p)

    result = evaluate([buy("XLK")], account, clean_risk, ctx, halted_cfg)
    assert result.halt_reason is not None
    assert result.flatten_all
    assert result.approved == []


def test_manual_halt_flag_blocks_entries(cfg, account, ctx):
    risk = RiskState(
        peak_equity=EQUITY, day_start_equity=EQUITY, month_start_equity=EQUITY, halted=True
    )
    result = evaluate([buy("XLK")], account, risk, ctx, cfg)
    assert result.halt_reason is not None
    assert result.approved == []


# --------------------------------------------------------------------------
# Universe filters — the user's current book must not pass these
# --------------------------------------------------------------------------


def test_sub_five_dollar_stock_is_rejected(cfg, account, clean_risk, ctx):
    result = evaluate([buy("PENNY", limit=2.50)], account, clean_risk, ctx, cfg)
    assert "below minimum" in only_rejection(result)


def test_illiquid_stock_is_rejected(cfg, account, clean_risk, ctx):
    result = evaluate([buy("THIN", limit=50.0)], account, clean_risk, ctx, cfg)
    assert "dollar volume" in only_rejection(result)


def test_tsmom_uses_its_smaller_asset_etf_liquidity_floor(
    cfg, account, clean_risk, ctx
):
    result = evaluate(
        [buy("THIN", limit=50.0, sleeve="tsmom")],
        account, clean_risk, ctx, cfg,
    )
    assert len(result.approved) == 1


def test_tsmom_liquidity_floor_requires_exact_sleeve_membership(cfg, account, clean_risk, ctx):
    """A sleeve name that merely contains "tsmom" as a substring must not
    inherit its lower liquidity floor — only an exact "tsmom" origin does.
    """
    result = evaluate(
        [buy("THIN", limit=50.0, sleeve="not_tsmom_at_all")],
        account, clean_risk, ctx, cfg,
    )
    assert only_rejection(result).startswith("20d avg dollar volume")


def test_tsmom_floor_applies_when_combined_with_another_sleeve(cfg, account, clean_risk, ctx):
    """A `+`-joined sleeve string still finds the exact "tsmom" part."""
    result = evaluate(
        [buy("THIN", limit=50.0, sleeve="tsmom+equity_core")],
        account, clean_risk, ctx, cfg,
    )
    assert len(result.approved) == 1


def test_recent_ipo_is_rejected(cfg, account, clean_risk, ctx):
    result = evaluate([buy("NEWIPO", limit=50.0)], account, clean_risk, ctx, cfg)
    assert "IPO" in only_rejection(result)


# --------------------------------------------------------------------------
# Position count, re-entry, leverage, cash
# --------------------------------------------------------------------------


def test_max_positions_is_enforced(cfg, clean_risk, ctx):
    positions = {
        f"SYM{i}": Position(f"SYM{i}", qty=1, avg_entry_price=10.0, current_price=11.0)
        for i in range(cfg.risk.max_positions)
    }
    account = AccountState(equity=EQUITY, cash=2000.0, positions=positions)
    result = evaluate([buy("XLK")], account, clean_risk, ctx, cfg)
    assert "max_positions" in only_rejection(result)


def test_revenge_trade_is_blocked(cfg, account, ctx):
    risk = RiskState(
        peak_equity=EQUITY,
        day_start_equity=EQUITY,
        month_start_equity=EQUITY,
        recent_losses={"XLK": dt.date(2026, 7, 20)},
    )
    result = evaluate([buy("XLK")], account, risk, ctx, cfg)
    assert "re-entry blocked" in only_rejection(result)


def test_reentry_allowed_once_the_block_expires(cfg, account, ctx):
    risk = RiskState(
        peak_equity=EQUITY,
        day_start_equity=EQUITY,
        month_start_equity=EQUITY,
        recent_losses={"XLK": dt.date(2026, 7, 10)},
    )
    result = evaluate([buy("XLK")], account, risk, ctx, cfg)
    assert len(result.approved) == 1


def test_leveraged_exposure_cap_shrinks_the_order(cfg, account, clean_risk, ctx):
    result = evaluate(
        [buy("TQQQ", notional=690.0, limit=50.0), buy("SOXL", notional=690.0, limit=20.0)],
        account,
        clean_risk,
        ctx,
        cfg,
    )
    lev_total = sum(o.notional for o in result.approved if o.symbol in {"TQQQ", "SOXL"})
    assert lev_total <= cfg.risk.max_leveraged_exposure_pct * EQUITY + 0.01


def test_orders_are_shrunk_to_available_cash(cfg, clean_risk, ctx):
    account = AccountState(equity=EQUITY, cash=100.0, positions={})
    result = evaluate([buy("XLK", notional=690.0)], account, clean_risk, ctx, cfg)
    assert len(result.approved) == 1
    assert result.approved[0].notional <= 100.0


# --------------------------------------------------------------------------
# Execution guards
# --------------------------------------------------------------------------


def test_limit_price_too_far_through_the_touch_is_rejected(cfg, account, clean_risk, ctx):
    # 2% above the reference price, limit is 0.30%
    result = evaluate([buy("XLK", limit=102.0)], account, clean_risk, ctx, cfg)
    assert "through the touch" in only_rejection(result)


def test_no_entries_in_the_opening_minutes(cfg, account, clean_risk, ctx):
    early = MarketContext(
        now=dt.datetime(2026, 7, 22, 9, 32, tzinfo=ET),
        is_trading_day=True,
        symbols=ctx.symbols,
    )
    result = evaluate([buy("XLK")], account, clean_risk, early, cfg)
    assert result.new_entries_blocked
    assert result.approved == []


def test_no_entries_in_the_closing_minutes(cfg, account, clean_risk, ctx):
    late = MarketContext(
        now=dt.datetime(2026, 7, 22, 15, 55, tzinfo=ET),
        is_trading_day=True,
        symbols=ctx.symbols,
    )
    result = evaluate([buy("XLK")], account, clean_risk, late, cfg)
    assert result.new_entries_blocked
    assert result.approved == []


def test_no_entries_on_a_market_holiday(cfg, account, clean_risk, ctx):
    holiday = MarketContext(
        now=dt.datetime(2026, 7, 3, 12, 30, tzinfo=ET),
        is_trading_day=False,
        symbols=ctx.symbols,
    )
    result = evaluate([buy("XLK")], account, clean_risk, holiday, cfg)
    assert result.new_entries_blocked
    assert result.approved == []


# --------------------------------------------------------------------------
# Exits are never blocked
# --------------------------------------------------------------------------


def test_sells_are_allowed_even_when_entries_are_blocked(cfg, ctx):
    account = AccountState(
        equity=3700.0,
        cash=0.0,
        positions={"XLK": Position("XLK", qty=5, avg_entry_price=110.0, current_price=100.0)},
    )
    risk = RiskState(peak_equity=EQUITY, day_start_equity=3700.0, month_start_equity=3700.0)
    sell = {"symbol": "XLK", "side": "sell", "sleeve": "momentum", "notional": 500.0, "limit_price": 100.0}
    result = evaluate([sell], account, risk, ctx, cfg)
    assert result.halt_reason is not None      # halted...
    assert len(result.approved) == 1           # ...but the exit still goes through
    assert result.approved[0].side == "sell"


def test_sell_of_unheld_symbol_is_rejected(cfg, account, clean_risk, ctx):
    sell = {"symbol": "XLK", "side": "sell", "sleeve": "momentum", "notional": 500.0, "limit_price": 100.0}
    result = evaluate([sell], account, clean_risk, ctx, cfg)
    assert "not held" in only_rejection(result)


def test_sell_cannot_exceed_held_quantity(cfg, clean_risk, ctx):
    account = AccountState(
        equity=EQUITY,
        cash=0.0,
        positions={"XLK": Position("XLK", qty=2, avg_entry_price=100.0, current_price=100.0)},
    )
    sell = {"symbol": "XLK", "side": "sell", "sleeve": "momentum", "notional": 5000.0, "limit_price": 100.0}
    result = evaluate([sell], account, clean_risk, ctx, cfg)
    assert result.approved[0].qty <= 2


# --------------------------------------------------------------------------
# Stop sizing
# --------------------------------------------------------------------------


def test_stop_uses_the_wider_of_flat_and_atr(cfg):
    # ATR 2% of price -> 2x = 4%, below the 8% floor, so the floor wins.
    assert stop_distance_pct(cfg, price=100.0, atr14=2.0) == pytest.approx(0.08)
    # ATR 6% of price -> 2x = 12%, above the floor, so ATR wins.
    assert stop_distance_pct(cfg, price=100.0, atr14=6.0) == pytest.approx(0.12)


def test_stop_is_capped_so_a_bad_atr_cannot_create_an_absurd_stop(cfg):
    assert stop_distance_pct(cfg, price=100.0, atr14=50.0) == pytest.approx(
        cfg.risk.max_stop_distance_pct
    )


# --------------------------------------------------------------------------
# Config validation — a bad config must halt, not loosen limits
# --------------------------------------------------------------------------


def _risk_limits(**overrides):
    from engine.config import RiskLimits

    base = dict(
        max_position_pct=0.15,
        max_leveraged_exposure_pct=0.20,
        max_long_exposure_pct=1.00,
        max_gross_exposure_pct=1.15,
        max_positions=120,
        stop_loss_pct=0.08,
        stop_atr_multiple=2.0,
        max_stop_distance_pct=0.15,
        daily_loss_limit_pct=0.04,
        monthly_kill_switch_pct=0.10,
        peak_drawdown_halt_pct=0.18,
        allow_averaging_down=False,
        loss_reentry_block_days=5,
        max_short_exposure_pct=0.15,
    )
    base.update(overrides)
    return RiskLimits(**base)


class TestPositionCapPct:
    def test_disabled_by_default(self):
        limits = _risk_limits()
        assert limits.position_cap_pct("equity_core+trend") == 0.15

    def test_widens_for_a_fully_qualifying_sleeve(self):
        limits = _risk_limits(
            elevated_position_pct=0.65,
            elevated_position_sleeves=frozenset({"equity_core", "trend"}),
        )
        assert limits.position_cap_pct("equity_core+trend") == 0.65
        assert limits.position_cap_pct("equity_core") == 0.65
        assert limits.position_cap_pct("trend") == 0.65

    def test_does_not_widen_a_partial_match(self):
        limits = _risk_limits(
            elevated_position_pct=0.65,
            elevated_position_sleeves=frozenset({"equity_core", "trend"}),
        )
        assert limits.position_cap_pct("equity_core+mom_ls") == 0.15
        assert limits.position_cap_pct("mom_ls") == 0.15

    def test_empty_sleeve_string_never_widens(self):
        limits = _risk_limits(
            elevated_position_pct=0.65,
            elevated_position_sleeves=frozenset({"equity_core", "trend"}),
        )
        assert limits.position_cap_pct("") == 0.15


def _cfg_with(tmp_path, **overrides):
    import yaml

    raw = yaml.safe_load(open("config.yaml"))
    for dotted, value in overrides.items():
        node = raw
        *parents, leaf = dotted.split(".")
        for key in parents:
            node = node[key]
        node[leaf] = value
    p = tmp_path / "mutated.yaml"
    p.write_text(yaml.safe_dump(raw))
    return p


@pytest.mark.parametrize(
    "override",
    [
        {"risk.allow_averaging_down": True},
        {"universe.allow_short": True, "risk.max_short_exposure_pct": 0},
        {"execution.order_type": "market"},
        {"mode": "yolo"},
        {"risk.max_position_pct": 1.5},
        {"risk.max_position_pct": 0},
        {"risk.max_positions": 0},
        {"account.starting_equity": -100},
        {"risk.peak_drawdown_halt_pct": 0.05},   # below the monthly switch — unreachable
        {"risk.max_stop_distance_pct": 0.02},    # below the stop floor
        # The shipped config already sets both together, so each of the next
        # two cases explicitly nulls the other field to isolate "one without
        # the other" rather than accidentally re-asserting the shipped pair.
        {"risk.elevated_position_pct": 0.65, "risk.elevated_position_sleeves": None},
        {"risk.elevated_position_pct": None, "risk.elevated_position_sleeves": ["equity_core", "trend"]},
        {"risk.elevated_position_pct": 0.10, "risk.elevated_position_sleeves": ["equity_core"]},  # narrows, not widens
        {"paper_portfolio.mom_ls_min_dollar_volume": 100},  # below the daily gate floor
    ],
)
def test_dangerous_config_is_rejected_at_load(tmp_path, override):
    with pytest.raises(ConfigError):
        load_config(_cfg_with(tmp_path, **override))


def test_shipped_config_loads_and_is_sane():
    cfg = load_config()
    assert cfg.mode in {"paper", "live", "halt"}
    assert not cfg.risk.allow_averaging_down
    # Shorting was enabled 2026-07-23 by explicit user authorization for the
    # Alpaca path. The invariant is no longer "no shorts" — it is "no shorts
    # without a gross exposure cap".
    if cfg.universe.allow_short:
        assert 0 < cfg.risk.max_short_exposure_pct <= 0.5
    assert cfg.execution.order_type == "limit"
    assert 0 < cfg.risk.max_position_pct <= 0.25


# --------------------------------------------------------------------------
# Shorts — enabled 2026-07-23; the gate rules that protect them
# --------------------------------------------------------------------------


def short_prop(symbol="XLK", notional=500.0, limit=100.0, sleeve="mom_ls"):
    return {"symbol": symbol, "side": "short", "sleeve": sleeve,
            "notional": notional, "limit_price": limit, "rationale": "test"}


# mom_ls is stop- and block-exempt in the shipped config, so tests that assert
# stop/block behaviour must name a sleeve that is still subject to them.
STOPPED_SLEEVE = "xsec_short"


@pytest.fixture
def ctx_shortable(ctx):
    symbols = dict(ctx.symbols)
    symbols["XLK"] = SymbolData(price=100.0, atr14=2.0, avg_dollar_volume_20d=500e6, shortable=True)
    symbols["NOBORROW"] = SymbolData(price=50.0, atr14=1.0, avg_dollar_volume_20d=100e6, shortable=False)
    symbols["THINSHORT"] = SymbolData(price=50.0, atr14=1.0, avg_dollar_volume_20d=1e6, shortable=True)
    symbols["NEWSHORT"] = SymbolData(
        price=50.0, atr14=1.0, avg_dollar_volume_20d=100e6, listed_days=30, shortable=True
    )
    return MarketContext(now=ctx.now, is_trading_day=True, symbols=symbols)


def test_short_carries_stop_above_entry(cfg, account, clean_risk, ctx_shortable):
    result = evaluate(
        [short_prop(sleeve=STOPPED_SLEEVE)], account, clean_risk, ctx_shortable, cfg
    )
    assert len(result.approved) == 1
    o = result.approved[0]
    assert o.side == "short"
    assert o.stop_price > o.limit_price          # stop ABOVE entry for shorts
    assert o.qty == int(o.qty)                   # whole shares only


def test_stop_exempt_sleeve_gets_no_stop(cfg, account, clean_risk, ctx_shortable):
    """mom_ls rebalances weekly; reports/risk_overlay_study.json says stopping
    it costs CAGR and Sharpe in both windows and both profiles."""
    assert "mom_ls" in cfg.risk.stop_exempt_sleeves
    result = evaluate([short_prop()], account, clean_risk, ctx_shortable, cfg)
    assert len(result.approved) == 1
    assert result.approved[0].stop_price == 0.0


def test_stop_exempt_buy_gets_no_stop(cfg, account, clean_risk, ctx):
    result = evaluate([buy(sleeve="mom_ls")], account, clean_risk, ctx, cfg)
    assert len(result.approved) == 1
    assert result.approved[0].stop_price == 0.0


def test_non_exempt_buy_still_carries_a_stop(cfg, account, clean_risk, ctx):
    result = evaluate([buy(sleeve="momentum")], account, clean_risk, ctx, cfg)
    assert len(result.approved) == 1
    o = result.approved[0]
    assert 0 < o.stop_price < o.limit_price


def test_short_rejected_when_not_shortable(cfg, account, clean_risk, ctx_shortable):
    result = evaluate([short_prop("NOBORROW", limit=50.0)], account, clean_risk, ctx_shortable, cfg)
    assert "not shortable" in only_rejection(result)


def test_short_rejected_when_account_does_not_permit_it(
    cfg, clean_risk, ctx_shortable
):
    account = AccountState(
        equity=EQUITY,
        cash=EQUITY,
        positions={},
        shorting_enabled=False,
    )
    result = evaluate([short_prop()], account, clean_risk, ctx_shortable, cfg)
    assert "account does not permit shorting" in only_rejection(result)


def test_short_rejects_illiquid_symbol(cfg, account, clean_risk, ctx_shortable):
    result = evaluate([short_prop("THINSHORT", limit=50.0)], account, clean_risk, ctx_shortable, cfg)
    assert "dollar volume" in only_rejection(result)


def test_short_rejects_recent_ipo(cfg, account, clean_risk, ctx_shortable):
    result = evaluate([short_prop("NEWSHORT", limit=50.0)], account, clean_risk, ctx_shortable, cfg)
    assert "IPO" in only_rejection(result)


def test_short_limit_too_far_through_touch_is_rejected(
    cfg, account, clean_risk, ctx_shortable
):
    result = evaluate([short_prop(limit=98.0)], account, clean_risk, ctx_shortable, cfg)
    assert "through the touch" in only_rejection(result)


def test_short_revenge_trade_is_blocked(cfg, account, ctx_shortable):
    risk = RiskState(
        peak_equity=EQUITY,
        day_start_equity=EQUITY,
        month_start_equity=EQUITY,
        recent_losses={"XLK": dt.date(2026, 7, 20)},
    )
    result = evaluate(
        [short_prop(sleeve=STOPPED_SLEEVE)], account, risk, ctx_shortable, cfg
    )
    assert "re-entry blocked" in only_rejection(result)


def test_reentry_block_exempt_sleeve_may_re_enter(cfg, account, ctx_shortable):
    """A systematic sleeve must not be barred from acting on its own signal."""
    assert "mom_ls" in cfg.risk.reentry_block_exempt_sleeves
    risk = RiskState(
        peak_equity=EQUITY,
        day_start_equity=EQUITY,
        month_start_equity=EQUITY,
        recent_losses={"XLK": dt.date(2026, 7, 20)},
    )
    result = evaluate([short_prop()], account, risk, ctx_shortable, cfg)
    assert len(result.approved) == 1


def test_short_gross_exposure_is_capped(cfg, account, clean_risk, ctx_shortable):
    props = [short_prop(notional=600.0) for _ in range(4)]
    result = evaluate(props, account, clean_risk, ctx_shortable, cfg)
    total = sum(o.notional for o in result.approved)
    assert total <= cfg.risk.max_short_exposure_pct * EQUITY + 100.0  # + one whole-share rounding


def test_short_blocked_when_entries_blocked(cfg, ctx_shortable):
    account = AccountState(equity=4370.0, cash=4370.0, positions={})
    risk = RiskState(peak_equity=EQUITY, day_start_equity=EQUITY, month_start_equity=EQUITY)
    result = evaluate([short_prop()], account, risk, ctx_shortable, cfg)
    assert result.approved == []


def test_cannot_short_a_symbol_held_long(cfg, clean_risk, ctx_shortable):
    account = AccountState(equity=EQUITY, cash=1000.0,
                           positions={"XLK": Position("XLK", 5, 90.0, 100.0)})
    result = evaluate([short_prop()], account, clean_risk, ctx_shortable, cfg)
    assert "held long" in only_rejection(result)


def test_averaging_down_on_a_losing_short_is_rejected(cfg, clean_risk, ctx_shortable):
    # Short from 90, price now 100 -> the short is LOSING. Adding is averaging down.
    account = AccountState(equity=EQUITY, cash=1000.0,
                           positions={"XLK": Position("XLK", -5, 90.0, 100.0)})
    result = evaluate([short_prop()], account, clean_risk, ctx_shortable, cfg)
    assert "averaging down" in only_rejection(result)


def test_averaging_down_on_a_losing_short_is_permitted_for_a_restoration_exempt_sleeve(
    cfg, clean_risk, ctx_shortable
):
    exempt_cfg = dataclasses.replace(
        cfg, risk=dataclasses.replace(cfg.risk, restoration_exempt_sleeves=frozenset({"mom_ls"}))
    )
    account = AccountState(equity=EQUITY, cash=1000.0,
                           positions={"XLK": Position("XLK", -5, 90.0, 100.0)})
    # short_prop() defaults sleeve="mom_ls".
    result = evaluate([short_prop()], account, clean_risk, ctx_shortable, exempt_cfg)
    assert len(result.approved) == 1
    assert len(result.rejected) == 0


def test_base_config_carries_no_restoration_exemption():
    """The base account is the unchanged control — this must stay empty
    there no matter what the 2x lab config does."""
    base = load_config()
    assert base.risk.restoration_exempt_sleeves == frozenset()


def test_cover_always_allowed_even_when_halted(cfg, ctx_shortable):
    account = AccountState(equity=3700.0, cash=5000.0,
                           positions={"XLK": Position("XLK", -5, 110.0, 100.0)})
    risk = RiskState(peak_equity=EQUITY, day_start_equity=3700.0, month_start_equity=3700.0)
    cover = {"symbol": "XLK", "side": "cover", "sleeve": "mom_ls",
             "notional": 500.0, "limit_price": 100.0}
    result = evaluate([cover], account, risk, ctx_shortable, cfg)
    assert result.halt_reason is not None
    assert len(result.approved) == 1 and result.approved[0].side == "cover"


def test_cover_without_a_short_is_rejected(cfg, account, clean_risk, ctx_shortable):
    cover = {"symbol": "XLK", "side": "cover", "sleeve": "mom_ls",
             "notional": 500.0, "limit_price": 100.0}
    result = evaluate([cover], account, clean_risk, ctx_shortable, cfg)
    assert "no short position" in only_rejection(result)


def test_short_loss_sign_is_correct():
    losing_short = Position("X", qty=-10, avg_entry_price=90.0, current_price=100.0)
    winning_short = Position("X", qty=-10, avg_entry_price=110.0, current_price=100.0)
    assert losing_short.unrealized_pct < 0
    assert winning_short.unrealized_pct > 0


# --------------------------------------------------------------------------
# Margin / leverage (the 2x profile)
# --------------------------------------------------------------------------


def test_buys_cap_at_buying_power_when_margined(cfg, clean_risk, ctx):
    """With buying_power set, buys spend margin headroom — not raw cash —
    but the gate still shrinks, never enlarges."""
    account = AccountState(equity=EQUITY, cash=0.0, positions={},
                           buying_power=2000.0)
    result = evaluate([buy("XLK", notional=5000.0)], account, clean_risk, ctx, cfg)
    assert len(result.approved) == 1
    assert result.approved[0].notional <= 2000.0


def test_cash_account_semantics_unchanged_when_buying_power_none(cfg, clean_risk, ctx):
    account = AccountState(equity=EQUITY, cash=100.0, positions={})
    result = evaluate([buy("XLK", notional=690.0)], account, clean_risk, ctx, cfg)
    assert result.approved[0].notional <= 100.0


def test_portfolio_long_exposure_cap_applies_even_with_buying_power(
    cfg, clean_risk, ctx
):
    existing = cfg.risk.max_long_exposure_pct * EQUITY - 50.0
    account = AccountState(
        equity=EQUITY,
        cash=0.0,
        positions={
            "EXISTING": Position(
                "EXISTING", qty=existing / 100.0,
                avg_entry_price=100.0, current_price=100.0
            )
        },
        buying_power=1000.0,
    )
    result = evaluate([buy("XLK", notional=500.0)], account, clean_risk, ctx, cfg)
    assert len(result.approved) == 1
    assert result.approved[0].notional <= 50.0


def test_sleeve_exemption_lists_must_be_lists_of_names():
    from engine.config import ConfigError, _sleeve_set

    assert _sleeve_set(None, "x") == frozenset()
    assert _sleeve_set(["mom_ls", " trend "], "x") == frozenset({"mom_ls", "trend"})
    # A typo must not silently disable a risk control.
    for bad in ("mom_ls", 5, ["mom_ls", ""], [None]):
        with pytest.raises(ConfigError):
            _sleeve_set(bad, "x")
