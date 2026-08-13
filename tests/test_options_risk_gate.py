"""Adversarial tests for engine/options_risk.py — the parallel gate for
multi-leg options structures. Mirrors tests/test_risk_gate.py's and
tests/test_experiment_tier.py's style: feed the gate a structure a buggy
selector or a misbehaving caller might realistically produce, and assert
it refuses it or shrinks it to size.
"""

from __future__ import annotations

import dataclasses
import datetime as dt

import pytest

from engine.config import ExperimentConfig, load_config
from engine.options_risk import (
    OptionLegQuote,
    OptionStructureProposal,
    _assert_option_gate_invariants,
    evaluate_option_structure,
)
from engine.risk import AccountState, RiskState

EQUITY = 10_000.0
EXPERIMENT_REGISTRATION = "reports/bull_put_delta_selected_shadow_launch.json"


@pytest.fixture
def cfg_with_experiment():
    base = load_config()
    experiment = ExperimentConfig(
        name="bull_put_delta_selected_live", status="paper", allocation_pct=0.06,
        max_cumulative_loss_pct=0.06, registration_path=EXPERIMENT_REGISTRATION,
    )
    return dataclasses.replace(base, experiments={"bull_put_delta_selected_live": experiment})


@pytest.fixture
def now():
    return dt.datetime(2026, 8, 12, 15, 0, tzinfo=dt.timezone.utc)


@pytest.fixture
def clean_risk():
    return RiskState(peak_equity=EQUITY, day_start_equity=EQUITY, month_start_equity=EQUITY)


@pytest.fixture
def account():
    return AccountState(equity=EQUITY, cash=EQUITY)


def legs(now, *, short_symbol="SPY260918P00744000", long_symbol="SPY260918P00739000",
         short_intent="sell_to_open", long_intent="buy_to_open",
         short_side="sell", long_side="buy", quote_ts=None):
    ts = quote_ts or now
    return (
        OptionLegQuote(short_symbol, short_side, short_intent, 1, ts, bid=4.46, ask=4.49),
        OptionLegQuote(long_symbol, long_side, long_intent, 1, ts, bid=3.77, ask=3.79),
    )


def proposal(now, *, sleeve="bull_put_delta_selected_live", contracts=1,
             credit=0.67, maximum_loss=473.0, is_closing=False, **leg_kwargs):
    return OptionStructureProposal(
        sleeve=sleeve, underlying="SPY", expiration_date=dt.date(2026, 9, 18),
        legs=legs(now, **leg_kwargs), contracts=contracts, credit=credit,
        maximum_loss=maximum_loss, is_closing=is_closing,
    )


def only_rejection(result):
    assert len(result.approved) == 0, f"expected rejection, got approval: {result.approved}"
    assert len(result.rejected) == 1
    return result.rejected[0].reason


# --------------------------------------------------------------------------
# Core contract
# --------------------------------------------------------------------------


def test_approves_a_clean_structure(cfg_with_experiment, account, clean_risk, now):
    result = evaluate_option_structure(
        proposal(now), account, clean_risk, cfg_with_experiment,
        now=now, new_entries_blocked=False,
    )
    assert len(result.approved) == 1
    assert len(result.rejected) == 0
    assert result.approved[0].contracts == 1


def test_no_governing_experiment_is_rejected(cfg_with_experiment, account, clean_risk, now):
    result = evaluate_option_structure(
        proposal(now, sleeve="unregistered_sleeve"), account, clean_risk,
        cfg_with_experiment, now=now, new_entries_blocked=False,
    )
    assert "no registered experiment" in only_rejection(result)


def test_no_governing_experiment_rejects_even_a_close(cfg_with_experiment, account, clean_risk, now):
    result = evaluate_option_structure(
        proposal(now, sleeve="unregistered_sleeve", is_closing=True,
                 short_intent="buy_to_close", long_intent="sell_to_close",
                 short_side="buy", long_side="sell"),
        account, clean_risk, cfg_with_experiment, now=now, new_entries_blocked=False,
    )
    assert "no registered experiment" in only_rejection(result)


# --------------------------------------------------------------------------
# Status / standdown / entry-window governance
# --------------------------------------------------------------------------


def test_shadow_status_never_places_a_real_order(cfg_with_experiment, account, clean_risk, now):
    experiment = dataclasses.replace(
        cfg_with_experiment.experiments["bull_put_delta_selected_live"], status="shadow"
    )
    cfg = dataclasses.replace(cfg_with_experiment, experiments={experiment.name: experiment})
    result = evaluate_option_structure(
        proposal(now), account, clean_risk, cfg, now=now, new_entries_blocked=False,
    )
    assert "not 'paper'" in only_rejection(result)


def test_off_status_rejects_new_entries(cfg_with_experiment, account, clean_risk, now):
    experiment = dataclasses.replace(
        cfg_with_experiment.experiments["bull_put_delta_selected_live"], status="off"
    )
    cfg = dataclasses.replace(cfg_with_experiment, experiments={experiment.name: experiment})
    result = evaluate_option_structure(
        proposal(now), account, clean_risk, cfg, now=now, new_entries_blocked=False,
    )
    assert "not 'paper'" in only_rejection(result)


def test_stood_down_experiment_rejects_new_entries(cfg_with_experiment, account, now):
    risk_state = RiskState(
        peak_equity=EQUITY, day_start_equity=EQUITY, month_start_equity=EQUITY,
        experiment_standdowns=frozenset({"bull_put_delta_selected_live"}),
    )
    result = evaluate_option_structure(
        proposal(now), account, risk_state, cfg_with_experiment,
        now=now, new_entries_blocked=False,
    )
    assert "stood down" in only_rejection(result)


def test_close_always_allowed_even_when_stood_down(cfg_with_experiment, account, now):
    risk_state = RiskState(
        peak_equity=EQUITY, day_start_equity=EQUITY, month_start_equity=EQUITY,
        experiment_standdowns=frozenset({"bull_put_delta_selected_live"}),
    )
    close_proposal = proposal(
        now, is_closing=True, credit=-0.20,
        short_intent="buy_to_close", long_intent="sell_to_close",
        short_side="buy", long_side="sell",
    )
    result = evaluate_option_structure(
        close_proposal, account, risk_state, cfg_with_experiment,
        now=now, new_entries_blocked=False,
    )
    assert len(result.approved) == 1


def test_new_entries_blocked_rejects(cfg_with_experiment, account, clean_risk, now):
    result = evaluate_option_structure(
        proposal(now), account, clean_risk, cfg_with_experiment,
        now=now, new_entries_blocked=True,
    )
    assert "new entries blocked" in only_rejection(result)


def test_close_ignores_new_entries_blocked(cfg_with_experiment, account, clean_risk, now):
    close_proposal = proposal(
        now, is_closing=True, credit=-0.20,
        short_intent="buy_to_close", long_intent="sell_to_close",
        short_side="buy", long_side="sell",
    )
    result = evaluate_option_structure(
        close_proposal, account, clean_risk, cfg_with_experiment,
        now=now, new_entries_blocked=True,
    )
    assert len(result.approved) == 1


def test_concurrency_cap_rejects_a_second_open_structure(cfg_with_experiment, account, clean_risk, now):
    result = evaluate_option_structure(
        proposal(now), account, clean_risk, cfg_with_experiment,
        now=now, new_entries_blocked=False, open_structure_count=1,
    )
    assert "already has" in only_rejection(result)


def test_concurrency_cap_does_not_apply_to_closes(cfg_with_experiment, account, clean_risk, now):
    close_proposal = proposal(
        now, is_closing=True, credit=-0.20,
        short_intent="buy_to_close", long_intent="sell_to_close",
        short_side="buy", long_side="sell",
    )
    result = evaluate_option_structure(
        close_proposal, account, clean_risk, cfg_with_experiment,
        now=now, new_entries_blocked=False, open_structure_count=1,
    )
    assert len(result.approved) == 1


# --------------------------------------------------------------------------
# Defined-risk / leg-shape / freshness
# --------------------------------------------------------------------------


@pytest.mark.parametrize("bad_loss", [float("inf"), float("nan"), 0.0, -100.0])
def test_undefined_or_non_positive_max_loss_is_rejected(cfg_with_experiment, account, clean_risk, now, bad_loss):
    result = evaluate_option_structure(
        proposal(now, maximum_loss=bad_loss), account, clean_risk, cfg_with_experiment,
        now=now, new_entries_blocked=False,
    )
    assert "undefined-risk" in only_rejection(result)


def test_too_few_legs_rejected(cfg_with_experiment, account, clean_risk, now):
    bad = dataclasses.replace(proposal(now), legs=legs(now)[:1])
    result = evaluate_option_structure(
        bad, account, clean_risk, cfg_with_experiment, now=now, new_entries_blocked=False,
    )
    assert "must have 2-4 legs" in only_rejection(result)


def test_closing_intent_on_a_non_closing_proposal_rejected(cfg_with_experiment, account, clean_risk, now):
    bad = dataclasses.replace(
        proposal(now),
        legs=legs(now, short_intent="buy_to_close", long_intent="buy_to_open"),
    )
    result = evaluate_option_structure(
        bad, account, clean_risk, cfg_with_experiment, now=now, new_entries_blocked=False,
    )
    assert "opening structure legs" in only_rejection(result)


def test_non_positive_quote_rejected(cfg_with_experiment, account, clean_risk, now):
    bad_legs = (
        OptionLegQuote("SPY260918P00744000", "sell", "sell_to_open", 1, now, bid=0.0, ask=4.49),
        OptionLegQuote("SPY260918P00739000", "buy", "buy_to_open", 1, now, bid=3.77, ask=3.79),
    )
    bad = dataclasses.replace(proposal(now), legs=bad_legs)
    result = evaluate_option_structure(
        bad, account, clean_risk, cfg_with_experiment, now=now, new_entries_blocked=False,
    )
    assert "non-positive quote" in only_rejection(result)


def test_stale_quote_rejected(cfg_with_experiment, account, clean_risk, now):
    stale = now - dt.timedelta(seconds=300)
    stale_legs = (
        OptionLegQuote("SPY260918P00744000", "sell", "sell_to_open", 1, stale, bid=4.46, ask=4.49),
        OptionLegQuote("SPY260918P00739000", "buy", "buy_to_open", 1, now, bid=3.77, ask=3.79),
    )
    bad = dataclasses.replace(proposal(now), legs=stale_legs)
    result = evaluate_option_structure(
        bad, account, clean_risk, cfg_with_experiment, now=now, new_entries_blocked=False,
    )
    assert "old, exceeds" in only_rejection(result)


def test_quote_within_freshness_window_is_fine(cfg_with_experiment, account, clean_risk, now):
    fresh = now - dt.timedelta(seconds=100)
    fresh_legs = (
        OptionLegQuote("SPY260918P00744000", "sell", "sell_to_open", 1, fresh, bid=4.46, ask=4.49),
        OptionLegQuote("SPY260918P00739000", "buy", "buy_to_open", 1, now, bid=3.77, ask=3.79),
    )
    ok = dataclasses.replace(proposal(now), legs=fresh_legs)
    result = evaluate_option_structure(
        ok, account, clean_risk, cfg_with_experiment, now=now, new_entries_blocked=False,
    )
    assert len(result.approved) == 1


# --------------------------------------------------------------------------
# Allocation cap
# --------------------------------------------------------------------------


def test_shrinks_contracts_to_allocation_cap(cfg_with_experiment, clean_risk, now):
    # allocation_pct=0.06 * 10_000 = $600 cap; structure max_loss=$473/contract.
    # Requesting 2 contracts ($946) should shrink to 1.
    account = AccountState(equity=EQUITY, cash=EQUITY)
    result = evaluate_option_structure(
        proposal(now, contracts=2), account, clean_risk, cfg_with_experiment,
        now=now, new_entries_blocked=False,
    )
    assert len(result.approved) == 1
    assert result.approved[0].contracts == 1
    assert result.approved[0].requested_contracts == 2
    assert result.approved[0].was_shrunk
    assert any("allocation cap" in a for a in result.approved[0].adjustments)


def test_rejects_when_no_contract_is_affordable(cfg_with_experiment, clean_risk, now):
    account = AccountState(
        equity=EQUITY, cash=EQUITY,
        experiment_gross_exposure={"bull_put_delta_selected_live": 600.0},
    )
    result = evaluate_option_structure(
        proposal(now), account, clean_risk, cfg_with_experiment,
        now=now, new_entries_blocked=False,
    )
    assert "allocation cap reached" in only_rejection(result)


def test_close_is_not_shrunk_by_allocation_cap(cfg_with_experiment, clean_risk, now):
    account = AccountState(
        equity=EQUITY, cash=EQUITY,
        experiment_gross_exposure={"bull_put_delta_selected_live": 600.0},
    )
    close_proposal = proposal(
        now, is_closing=True, credit=-0.20, contracts=3,
        short_intent="buy_to_close", long_intent="sell_to_close",
        short_side="buy", long_side="sell",
    )
    result = evaluate_option_structure(
        close_proposal, account, clean_risk, cfg_with_experiment,
        now=now, new_entries_blocked=False,
    )
    assert len(result.approved) == 1
    assert result.approved[0].contracts == 3  # never shrunk


# --------------------------------------------------------------------------
# Runtime invariants (defense in depth)
# --------------------------------------------------------------------------


def _approved_structure(cfg, name="bull_put_delta_selected_live", *, contracts=1, requested=1,
                        maximum_loss=473.0, is_closing=False):
    import engine.options_risk as options_risk_mod
    from engine.execute import OptionLeg
    return options_risk_mod.ApprovedOptionStructure(
        sleeve=name, underlying="SPY", expiration_date=dt.date(2026, 9, 18),
        legs=(OptionLeg("A", "sell", "sell_to_open", 1), OptionLeg("B", "buy", "buy_to_open", 1)),
        contracts=contracts, requested_contracts=requested, credit=0.67,
        maximum_loss=maximum_loss, is_closing=is_closing,
    )


def test_invariant_catches_enlarged_contracts(cfg_with_experiment, clean_risk):
    import engine.options_risk as options_risk_mod
    result = options_risk_mod.OptionGateResult(
        approved=[_approved_structure(cfg_with_experiment, contracts=2, requested=1)]
    )
    with pytest.raises(AssertionError, match="approved 2 contracts"):
        _assert_option_gate_invariants(result, cfg_with_experiment, clean_risk)


def test_invariant_catches_undefined_risk_approval(cfg_with_experiment, clean_risk):
    import engine.options_risk as options_risk_mod
    result = options_risk_mod.OptionGateResult(
        approved=[_approved_structure(cfg_with_experiment, maximum_loss=float("inf"))]
    )
    with pytest.raises(AssertionError, match="non-finite or non-positive"):
        _assert_option_gate_invariants(result, cfg_with_experiment, clean_risk)


def test_invariant_catches_shadow_status_bypassing_inline_check(cfg_with_experiment, clean_risk):
    import engine.options_risk as options_risk_mod
    shadow_cfg = dataclasses.replace(
        cfg_with_experiment,
        experiments={"bull_put_delta_selected_live": dataclasses.replace(
            cfg_with_experiment.experiments["bull_put_delta_selected_live"], status="shadow"
        )},
    )
    result = options_risk_mod.OptionGateResult(approved=[_approved_structure(shadow_cfg)])
    with pytest.raises(AssertionError, match="not 'paper'"):
        _assert_option_gate_invariants(result, shadow_cfg, clean_risk)


def test_invariant_catches_standdown_bypassing_inline_check(cfg_with_experiment):
    import engine.options_risk as options_risk_mod
    risk_state = RiskState(
        peak_equity=EQUITY, day_start_equity=EQUITY, month_start_equity=EQUITY,
        experiment_standdowns=frozenset({"bull_put_delta_selected_live"}),
    )
    result = options_risk_mod.OptionGateResult(approved=[_approved_structure(cfg_with_experiment)])
    with pytest.raises(AssertionError, match="stood-down"):
        _assert_option_gate_invariants(result, cfg_with_experiment, risk_state)


def test_invariant_allows_a_closing_structure_regardless_of_status(cfg_with_experiment, clean_risk):
    import engine.options_risk as options_risk_mod
    off_cfg = dataclasses.replace(
        cfg_with_experiment,
        experiments={"bull_put_delta_selected_live": dataclasses.replace(
            cfg_with_experiment.experiments["bull_put_delta_selected_live"], status="off"
        )},
    )
    result = options_risk_mod.OptionGateResult(
        approved=[_approved_structure(off_cfg, is_closing=True)]
    )
    _assert_option_gate_invariants(result, off_cfg, clean_risk)  # must not raise
