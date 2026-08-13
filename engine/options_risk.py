"""The options gate. Parallel to engine/risk.py's evaluate(), not part of it.

engine/risk.py's evaluate() has exactly two powers over a single-symbol
equity proposal: reject, or shrink. This module gives the same two powers
to a multi-leg options structure, with its own invariants — a structure
this gate approves must always carry a finite, positive, already-bounded
maximum loss; nothing here can turn an undefined-risk structure into an
approved order.

Kept in its own file rather than folded into engine/risk.py's evaluate() on
purpose: the equity gate's contract, tests, and blast radius stay at
exactly zero risk from anything here. This module reuses (does not
reimplement) engine/risk.py's experiment-tier building blocks —
ExperimentConfig, _experiment_for_sleeve, compute_experiment_standdowns —
rather than inventing a second copy of that governance.

Options are experiment-gated only: a proposal whose sleeve does not resolve
to a registered experiment is rejected outright. There is no "core options
allocation" the way there is for equities.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field
from typing import Any

from engine.config import Config
from engine.execute import OptionLeg
from engine.risk import AccountState, RiskState, _experiment_for_sleeve

_OPENING_INTENTS = frozenset({"buy_to_open", "sell_to_open"})
_CLOSING_INTENTS = frozenset({"buy_to_close", "sell_to_close"})
MAX_LEGS = 4
MIN_LEGS = 2
# At most one open structure per experiment at a time — bounds worst-case
# loss to exactly one allocation_pct by construction and keeps reconciliation
# simple (0-or-1 structure to check, never N). No laddering, no rolling.
MAX_CONCURRENT_STRUCTURES_PER_EXPERIMENT = 1


@dataclass(frozen=True)
class OptionLegQuote:
    """One leg of a proposed structure, with the quote it was priced from."""

    symbol: str
    side: str
    position_intent: str
    ratio_qty: int
    quote_ts: dt.datetime          # timezone-aware
    bid: float
    ask: float


@dataclass(frozen=True)
class OptionStructureProposal:
    """An options structure the caller wants approved.

    ``credit``/``maximum_loss`` are PER CONTRACT, matching
    scripts/options_shadow.py's existing selector convention exactly —
    ``credit`` positive means a net credit received, negative a net debit
    paid; ``maximum_loss`` is always a positive dollar figure (or must be,
    to pass this gate at all).
    """

    sleeve: str
    underlying: str
    expiration_date: dt.date
    legs: tuple[OptionLegQuote, ...]
    contracts: int
    credit: float
    maximum_loss: float
    is_closing: bool = False
    rationale: str = ""


@dataclass
class ApprovedOptionStructure:
    sleeve: str
    underlying: str
    expiration_date: dt.date
    legs: tuple[OptionLeg, ...]      # ready to hand to Trader.submit_multi_leg_order
    contracts: int
    requested_contracts: int
    credit: float                    # per contract
    maximum_loss: float              # per contract
    is_closing: bool = False
    adjustments: list[str] = field(default_factory=list)

    @property
    def was_shrunk(self) -> bool:
        return self.contracts < self.requested_contracts


@dataclass
class RejectedOptionStructure:
    underlying: str
    reason: str
    raw: Any = None


@dataclass
class OptionGateResult:
    approved: list[ApprovedOptionStructure] = field(default_factory=list)
    rejected: list[RejectedOptionStructure] = field(default_factory=list)


def _leg_shape_error(proposal: OptionStructureProposal) -> str | None:
    legs = proposal.legs
    if not (MIN_LEGS <= len(legs) <= MAX_LEGS):
        return f"structure must have {MIN_LEGS}-{MAX_LEGS} legs, got {len(legs)}"
    intents = {leg.position_intent for leg in legs}
    expected = _CLOSING_INTENTS if proposal.is_closing else _OPENING_INTENTS
    if not intents <= expected:
        kind = "closing" if proposal.is_closing else "opening"
        return f"{kind} structure legs must all use {sorted(expected)}, got {sorted(intents)}"
    for leg in legs:
        if leg.side not in ("buy", "sell"):
            return f"{leg.symbol}: side must be buy/sell, got {leg.side!r}"
        if not (isinstance(leg.ratio_qty, int) and leg.ratio_qty > 0):
            return f"{leg.symbol}: ratio_qty must be a positive int, got {leg.ratio_qty!r}"
    return None


def _stale_quote_error(
    proposal: OptionStructureProposal, *, now: dt.datetime, max_quote_age_seconds: float
) -> str | None:
    for leg in proposal.legs:
        if leg.bid <= 0 or leg.ask <= 0:
            return f"{leg.symbol}: non-positive quote (bid={leg.bid}, ask={leg.ask})"
        age = (now - leg.quote_ts).total_seconds()
        if age > max_quote_age_seconds:
            return (
                f"{leg.symbol}: quote is {age:.0f}s old, exceeds "
                f"{max_quote_age_seconds:.0f}s freshness limit"
            )
    return None


def evaluate_option_structure(
    proposal: OptionStructureProposal,
    account: AccountState,
    risk_state: RiskState,
    cfg: Config,
    *,
    now: dt.datetime,
    new_entries_blocked: bool,
    open_structure_count: int = 0,
    max_quote_age_seconds: float = 120.0,
) -> OptionGateResult:
    """Evaluate one options structure proposal. Reject or shrink; never enlarge.

    ``new_entries_blocked`` is computed externally by calling
    engine.risk.evaluate(proposals=[], account, risk_state, ctx, cfg) and
    reading result.new_entries_blocked — this reuses the equity gate's
    halt/drawdown/daily-loss/entry-window logic instead of re-deriving it
    a second time. ``open_structure_count`` is computed externally from the
    options journal (how many structures are already open/pending for this
    proposal's experiment) — this gate is pure/stateless and does not touch
    a database.
    """
    result = OptionGateResult()

    experiment = _experiment_for_sleeve(cfg, proposal.sleeve)
    if experiment is None:
        result.rejected.append(RejectedOptionStructure(
            proposal.underlying,
            f"sleeve {proposal.sleeve!r} resolves to no registered experiment; "
            "options are experiment-gated only",
            proposal,
        ))
        return result

    if not proposal.is_closing:
        if experiment.name in risk_state.experiment_standdowns:
            result.rejected.append(RejectedOptionStructure(
                proposal.underlying,
                f"experiment {experiment.name!r} stood down: cumulative loss limit breached",
                proposal,
            ))
            return result
        if experiment.status != "paper":
            result.rejected.append(RejectedOptionStructure(
                proposal.underlying,
                f"experiment {experiment.name!r} is {experiment.status!r}, not 'paper'; "
                "no real options orders permitted",
                proposal,
            ))
            return result
        if new_entries_blocked:
            result.rejected.append(RejectedOptionStructure(
                proposal.underlying, "new entries blocked", proposal,
            ))
            return result
        if open_structure_count >= MAX_CONCURRENT_STRUCTURES_PER_EXPERIMENT:
            result.rejected.append(RejectedOptionStructure(
                proposal.underlying,
                f"experiment {experiment.name!r} already has "
                f"{open_structure_count} open structure(s); cap is "
                f"{MAX_CONCURRENT_STRUCTURES_PER_EXPERIMENT}",
                proposal,
            ))
            return result

    shape_error = _leg_shape_error(proposal)
    if shape_error:
        result.rejected.append(RejectedOptionStructure(proposal.underlying, shape_error, proposal))
        return result

    if not math.isfinite(proposal.maximum_loss) or proposal.maximum_loss <= 0:
        result.rejected.append(RejectedOptionStructure(
            proposal.underlying,
            "structure carries no bounded maximum_loss; refusing an "
            "undefined-risk options order",
            proposal,
        ))
        return result

    quote_error = _stale_quote_error(
        proposal, now=now, max_quote_age_seconds=max_quote_age_seconds
    )
    if quote_error:
        result.rejected.append(RejectedOptionStructure(proposal.underlying, quote_error, proposal))
        return result

    adjustments: list[str] = []
    contracts = proposal.contracts

    if not proposal.is_closing:
        exp_cap = experiment.allocation_pct * account.equity
        exp_committed = account.experiment_gross_exposure.get(experiment.name, 0.0)
        exp_room = exp_cap - exp_committed
        max_affordable = math.floor(exp_room / proposal.maximum_loss) if exp_room > 0 else 0
        if max_affordable < 1:
            result.rejected.append(RejectedOptionStructure(
                proposal.underlying,
                f"experiment {experiment.name!r} allocation cap reached "
                f"({exp_cap:,.2f}; room {exp_room:,.2f} < one contract's "
                f"maximum_loss {proposal.maximum_loss:,.2f})",
                proposal,
            ))
            return result
        if max_affordable < contracts:
            adjustments.append(
                f"shrunk to experiment {experiment.name!r} allocation cap "
                f"({contracts} -> {max_affordable} contracts)"
            )
            contracts = max_affordable

    legs = tuple(
        OptionLeg(leg.symbol, leg.side, leg.position_intent, leg.ratio_qty)
        for leg in proposal.legs
    )
    result.approved.append(ApprovedOptionStructure(
        sleeve=proposal.sleeve,
        underlying=proposal.underlying,
        expiration_date=proposal.expiration_date,
        legs=legs,
        contracts=contracts,
        requested_contracts=proposal.contracts,
        credit=proposal.credit,
        maximum_loss=proposal.maximum_loss,
        is_closing=proposal.is_closing,
        adjustments=adjustments,
    ))
    _assert_option_gate_invariants(result, cfg, risk_state)
    return result


def _assert_option_gate_invariants(
    result: OptionGateResult, cfg: Config, risk_state: RiskState
) -> None:
    """The options gate's contract, enforced at runtime as well as in tests.

    Mirrors engine.risk._assert_gate_invariants: a bug that let this gate
    enlarge a structure or approve undefined risk would be the single most
    expensive failure mode here, so it fails loudly rather than reaching
    the broker.
    """
    for approved in result.approved:
        if approved.contracts > approved.requested_contracts:
            raise AssertionError(
                f"OPTIONS GATE INVARIANT VIOLATED: {approved.underlying} approved "
                f"{approved.contracts} contracts > requested {approved.requested_contracts}"
            )
        if approved.contracts < 1 or int(approved.contracts) != approved.contracts:
            raise AssertionError(
                f"OPTIONS GATE INVARIANT VIOLATED: {approved.underlying} approved "
                f"non-positive or fractional contracts {approved.contracts}"
            )
        if not math.isfinite(approved.maximum_loss) or approved.maximum_loss <= 0:
            raise AssertionError(
                f"OPTIONS GATE INVARIANT VIOLATED: {approved.underlying} approved "
                f"with non-finite or non-positive maximum_loss {approved.maximum_loss}"
            )
        if not approved.is_closing:
            experiment = _experiment_for_sleeve(cfg, approved.sleeve)
            if experiment is None or experiment.status != "paper":
                raise AssertionError(
                    f"OPTIONS GATE INVARIANT VIOLATED: {approved.underlying} approved "
                    f"for experiment {approved.sleeve!r} whose status is not 'paper'"
                )
            if experiment.name in risk_state.experiment_standdowns:
                raise AssertionError(
                    f"OPTIONS GATE INVARIANT VIOLATED: {approved.underlying} approved "
                    f"for stood-down experiment {experiment.name!r}"
                )
