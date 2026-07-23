"""THE GATE.

Pure Python, no LLM. This is the only component that decides what actually
reaches the broker, and it has exactly two powers: **reject** a proposal, or
**shrink** it. It can never enlarge a proposal and never invent one.

That invariant is asserted at the end of `evaluate()` and covered by
`tests/test_risk_gate.py`. If you change this file, those tests gate the change.

The analyst LLM writes `state/proposals.json`. It has no broker credentials and
no way to bypass this module.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field, asdict
from typing import Any, Iterable

from engine.config import Config

VALID_SIDES = {"buy", "sell", "short", "cover"}


# --------------------------------------------------------------------------
# Inputs
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Position:
    symbol: str
    qty: float
    avg_entry_price: float
    current_price: float

    @property
    def market_value(self) -> float:
        return self.qty * self.current_price

    @property
    def is_short(self) -> bool:
        return self.qty < 0

    @property
    def unrealized_pct(self) -> float:
        """Signed P&L fraction. For a short, a price RISE is the loss."""
        if self.avg_entry_price <= 0:
            return 0.0
        raw = (self.current_price - self.avg_entry_price) / self.avg_entry_price
        return -raw if self.is_short else raw


@dataclass(frozen=True)
class AccountState:
    equity: float
    cash: float
    positions: dict[str, Position] = field(default_factory=dict)
    # Margin headroom for buys. None = cash account semantics (cap at cash).
    # The daily runner computes this as (gross_leverage * equity - long
    # exposure), NOT the broker's 4x buying power — the config is the cap.
    buying_power: float | None = None
    shorting_enabled: bool = True

    def short_exposure(self) -> float:
        return sum(abs(p.market_value) for p in self.positions.values() if p.is_short)

    def long_exposure(self) -> float:
        return sum(p.market_value for p in self.positions.values() if not p.is_short)

    def gross_exposure(self) -> float:
        return self.long_exposure() + self.short_exposure()

    def leveraged_exposure(self, leveraged_symbols: Iterable[str]) -> float:
        lev = set(leveraged_symbols)
        return sum(p.market_value for s, p in self.positions.items() if s in lev)


@dataclass(frozen=True)
class RiskState:
    """Running P&L context the gate needs to evaluate the kill switches."""

    peak_equity: float
    day_start_equity: float
    month_start_equity: float
    # symbol -> date of the most recent losing exit
    recent_losses: dict[str, dt.date] = field(default_factory=dict)
    # Set once the absolute halt has fired; cleared only by a human.
    halted: bool = False


@dataclass(frozen=True)
class SymbolData:
    """Market facts the gate validates a proposal against."""

    price: float
    atr14: float
    avg_dollar_volume_20d: float
    listed_days: int = 10_000
    is_leveraged: bool = False
    shortable: bool = False


@dataclass(frozen=True)
class MarketContext:
    now: dt.datetime          # timezone-aware, exchange timezone
    is_trading_day: bool
    symbols: dict[str, SymbolData] = field(default_factory=dict)


# --------------------------------------------------------------------------
# Outputs
# --------------------------------------------------------------------------


@dataclass
class ApprovedOrder:
    symbol: str
    side: str
    sleeve: str
    notional: float
    qty: float
    limit_price: float
    stop_price: float
    requested_notional: float
    adjustments: list[str] = field(default_factory=list)

    @property
    def was_shrunk(self) -> bool:
        return self.notional < self.requested_notional - 1e-9


@dataclass
class RejectedProposal:
    symbol: str
    reason: str
    raw: Any = None


@dataclass
class GateResult:
    approved: list[ApprovedOrder] = field(default_factory=list)
    rejected: list[RejectedProposal] = field(default_factory=list)
    halt_reason: str | None = None
    flatten_all: bool = False
    new_entries_blocked: bool = False
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "approved": [asdict(o) for o in self.approved],
            "rejected": [asdict(r) for r in self.rejected],
            "halt_reason": self.halt_reason,
            "flatten_all": self.flatten_all,
            "new_entries_blocked": self.new_entries_blocked,
            "notes": self.notes,
        }


# --------------------------------------------------------------------------
# Gate
# --------------------------------------------------------------------------


def stop_distance_pct(cfg: Config, price: float, atr14: float) -> float:
    """Stop distance as a positive fraction of price.

    Wider of the flat floor and the ATR-based distance, so a volatile name gets
    room to breathe instead of being stopped out by noise — then capped, so a
    bad ATR reading can't produce an absurd stop.
    """
    atr_pct = (cfg.risk.stop_atr_multiple * atr14 / price) if price > 0 else 0.0
    return min(max(cfg.risk.stop_loss_pct, atr_pct), cfg.risk.max_stop_distance_pct)


def _parse_proposal(raw: Any) -> tuple[dict | None, str | None]:
    """Validate one proposal's shape. Returns (clean, error)."""
    if not isinstance(raw, dict):
        return None, f"proposal is not an object: {type(raw).__name__}"

    symbol = raw.get("symbol")
    if not isinstance(symbol, str) or not symbol.strip():
        return None, "missing or invalid 'symbol'"
    symbol = symbol.strip().upper()

    side = raw.get("side")
    if side not in VALID_SIDES:
        return None, f"side must be one of {sorted(VALID_SIDES)}, got {side!r}"

    notional = raw.get("notional")
    if isinstance(notional, bool) or not isinstance(notional, (int, float)):
        return None, f"'notional' must be a number, got {notional!r}"
    notional = float(notional)
    if not math.isfinite(notional) or notional <= 0:
        return None, f"'notional' must be finite and positive, got {notional}"

    limit_price = raw.get("limit_price")
    if isinstance(limit_price, bool) or not isinstance(limit_price, (int, float)):
        return None, f"'limit_price' must be a number, got {limit_price!r}"
    limit_price = float(limit_price)
    if not math.isfinite(limit_price) or limit_price <= 0:
        return None, f"'limit_price' must be finite and positive, got {limit_price}"

    sleeve = raw.get("sleeve")
    if not isinstance(sleeve, str) or not sleeve.strip():
        return None, "missing or invalid 'sleeve'"

    return {
        "symbol": symbol,
        "side": side,
        "sleeve": sleeve.strip(),
        "notional": notional,
        "limit_price": limit_price,
        "rationale": raw.get("rationale", ""),
    }, None


def _entry_window_open(cfg: Config, ctx: MarketContext) -> tuple[bool, str]:
    if not ctx.is_trading_day:
        return False, "not a trading day"

    now_t = ctx.now.timetz().replace(tzinfo=None)
    open_t, close_t = cfg.execution.market_open, cfg.execution.market_close
    today = ctx.now.date()
    open_dt = dt.datetime.combine(today, open_t)
    close_dt = dt.datetime.combine(today, close_t)
    now_dt = dt.datetime.combine(today, now_t)

    earliest = open_dt + dt.timedelta(minutes=cfg.execution.no_entry_first_minutes)
    latest = close_dt - dt.timedelta(minutes=cfg.execution.no_entry_last_minutes)

    if now_dt < earliest:
        return False, (
            f"entries blocked for the first {cfg.execution.no_entry_first_minutes} minutes "
            f"(now {now_t.strftime('%H:%M')}, entries open {earliest.time().strftime('%H:%M')})"
        )
    if now_dt > latest:
        return False, (
            f"entries blocked for the last {cfg.execution.no_entry_last_minutes} minutes "
            f"(now {now_t.strftime('%H:%M')}, entries closed {latest.time().strftime('%H:%M')})"
        )
    return True, ""


def evaluate(
    proposals: Any,
    account: AccountState,
    risk_state: RiskState,
    ctx: MarketContext,
    cfg: Config,
) -> GateResult:
    """Run every proposal through the gate.

    `proposals` is untrusted — it is whatever the analyst LLM wrote to disk.
    Anything that is not a well-formed list of well-formed objects is rejected,
    not coerced.
    """
    result = GateResult()

    # ---- Global circuit breakers, checked before any individual proposal ----

    if cfg.mode == "halt":
        result.halt_reason = "config mode is 'halt'"
        result.flatten_all = True
        result.new_entries_blocked = True

    if risk_state.halted:
        result.halt_reason = result.halt_reason or "risk state is halted; manual restart required"
        result.new_entries_blocked = True

    peak = max(risk_state.peak_equity, account.equity)
    if peak > 0:
        drawdown = (peak - account.equity) / peak
        if drawdown >= cfg.risk.peak_drawdown_halt_pct:
            result.halt_reason = (
                f"peak drawdown {drawdown:.2%} >= limit {cfg.risk.peak_drawdown_halt_pct:.2%} "
                "— halted, manual restart required"
            )
            result.flatten_all = True
            result.new_entries_blocked = True

    if risk_state.month_start_equity > 0:
        mtd = (account.equity - risk_state.month_start_equity) / risk_state.month_start_equity
        if mtd <= -cfg.risk.monthly_kill_switch_pct:
            result.notes.append(
                f"monthly kill switch: MTD {mtd:.2%} <= -{cfg.risk.monthly_kill_switch_pct:.2%}; "
                "flattening to cash until the 1st"
            )
            result.flatten_all = True
            result.new_entries_blocked = True

    if risk_state.day_start_equity > 0:
        daily = (account.equity - risk_state.day_start_equity) / risk_state.day_start_equity
        if daily <= -cfg.risk.daily_loss_limit_pct:
            result.notes.append(
                f"daily loss limit: {daily:.2%} <= -{cfg.risk.daily_loss_limit_pct:.2%}; "
                "no new entries for the rest of the day"
            )
            result.new_entries_blocked = True

    window_open, window_reason = _entry_window_open(cfg, ctx)
    if not window_open:
        result.new_entries_blocked = True
        result.notes.append(window_reason)

    # ---- Proposal list shape ----

    if proposals is None:
        proposals = []
    if isinstance(proposals, dict):
        proposals = proposals.get("proposals", None)
    if not isinstance(proposals, list):
        result.rejected.append(
            RejectedProposal(symbol="?", reason=f"proposals must be a list, got {type(proposals).__name__}", raw=proposals)
        )
        return result

    # ---- Per-proposal ----

    open_position_count = len(account.positions)
    committed_notional: dict[str, float] = {}
    leveraged_exposure = account.leveraged_exposure(cfg.leveraged_symbols)
    long_exposure = account.long_exposure()
    gross_exposure = account.gross_exposure()
    cash_remaining = account.cash if account.buying_power is None else account.buying_power

    for raw in proposals:
        clean, err = _parse_proposal(raw)
        if err:
            result.rejected.append(RejectedProposal(symbol="?", reason=err, raw=raw))
            continue

        symbol = clean["symbol"]
        requested = clean["notional"]
        adjustments: list[str] = []

        # Covers close short risk — always allowed through, like sells.
        if clean["side"] == "cover":
            held = account.positions.get(symbol)
            if held is None or not held.is_short:
                result.rejected.append(
                    RejectedProposal(symbol, "cover proposed but no short position held", raw)
                )
                continue
            result.approved.append(
                ApprovedOrder(
                    symbol=symbol, side="cover", sleeve=clean["sleeve"],
                    notional=min(requested, abs(held.market_value)),
                    qty=min(requested / clean["limit_price"], abs(held.qty)),
                    limit_price=clean["limit_price"], stop_price=0.0,
                    requested_notional=requested,
                    adjustments=["capped at held quantity"] if requested > abs(held.market_value) else [],
                )
            )
            continue

        # Sells (exits) are always allowed through — closing risk is never blocked.
        if clean["side"] == "sell":
            held = account.positions.get(symbol)
            if held is None:
                result.rejected.append(
                    RejectedProposal(symbol, "sell proposed for a symbol not held", raw)
                )
                continue
            result.approved.append(
                ApprovedOrder(
                    symbol=symbol,
                    side="sell",
                    sleeve=clean["sleeve"],
                    notional=min(requested, held.market_value),
                    qty=min(requested / clean["limit_price"], held.qty),
                    limit_price=clean["limit_price"],
                    stop_price=0.0,
                    requested_notional=requested,
                    adjustments=["capped at held quantity"] if requested > held.market_value else [],
                )
            )
            continue

        # --- From here: entries (buys and shorts) ---

        if clean["side"] == "short":
            if not cfg.universe.allow_short:
                result.rejected.append(RejectedProposal(symbol, "shorting disabled in config", raw))
                continue
            if not account.shorting_enabled:
                result.rejected.append(
                    RejectedProposal(symbol, "broker account does not permit shorting", raw)
                )
                continue
            if result.new_entries_blocked:
                result.rejected.append(
                    RejectedProposal(symbol, f"new entries blocked: {'; '.join(result.notes) or result.halt_reason}", raw)
                )
                continue
            data = ctx.symbols.get(symbol)
            if data is None:
                result.rejected.append(RejectedProposal(symbol, "no market data for symbol; cannot validate", raw))
                continue
            if not data.shortable:
                result.rejected.append(RejectedProposal(symbol, "not shortable / hard to borrow", raw))
                continue
            if data.price < cfg.universe.min_price:
                result.rejected.append(
                    RejectedProposal(symbol, f"price {data.price:.2f} below minimum {cfg.universe.min_price:.2f}", raw))
                continue
            if data.avg_dollar_volume_20d < cfg.universe.min_avg_dollar_volume:
                result.rejected.append(
                    RejectedProposal(
                        symbol,
                        f"20d avg dollar volume {data.avg_dollar_volume_20d:,.0f} below minimum "
                        f"{cfg.universe.min_avg_dollar_volume:,.0f}",
                        raw,
                    )
                )
                continue
            if data.listed_days < cfg.universe.exclude_ipo_days:
                result.rejected.append(
                    RejectedProposal(
                        symbol,
                        f"listed {data.listed_days}d < {cfg.universe.exclude_ipo_days}d IPO exclusion",
                        raw,
                    )
                )
                continue
            slip = (data.price - clean["limit_price"]) / data.price if data.price > 0 else 0.0
            if slip > cfg.execution.max_limit_slippage_pct:
                result.rejected.append(
                    RejectedProposal(
                        symbol,
                        f"short limit price {clean['limit_price']:.4f} is {slip:.2%} through the touch, "
                        f"limit {cfg.execution.max_limit_slippage_pct:.2%}",
                        raw,
                    )
                )
                continue
            held = account.positions.get(symbol)
            if held is not None and not held.is_short:
                result.rejected.append(RejectedProposal(symbol, "cannot short a symbol held long (would be a wash)", raw))
                continue
            if held is not None and held.is_short and held.unrealized_pct < 0:
                result.rejected.append(
                    RejectedProposal(symbol, f"averaging down is never permitted (short is {held.unrealized_pct:.2%})", raw))
                continue
            loss_date = risk_state.recent_losses.get(symbol)
            if loss_date is not None:
                age = (ctx.now.date() - loss_date).days
                if age < cfg.risk.loss_reentry_block_days:
                    result.rejected.append(
                        RejectedProposal(
                            symbol,
                            f"exited at a loss {age}d ago; re-entry blocked for "
                            f"{cfg.risk.loss_reentry_block_days}d",
                            raw,
                        )
                    )
                    continue
            if held is None and open_position_count >= cfg.risk.max_positions:
                result.rejected.append(RejectedProposal(symbol, f"already at max_positions ({cfg.risk.max_positions})", raw))
                continue

            short_cap = cfg.risk.max_short_exposure_pct * account.equity
            total_committed_short = sum(v for k, v in committed_notional.items() if k.startswith("SHORT:"))
            short_room = short_cap - account.short_exposure() - total_committed_short
            gross_cap = cfg.risk.max_gross_exposure_pct * account.equity
            gross_room = gross_cap - gross_exposure - total_committed_short
            room = min(short_room, gross_room)
            if room <= 0:
                result.rejected.append(
                    RejectedProposal(
                        symbol,
                        f"short or gross exposure cap reached "
                        f"(short {short_cap:,.2f}, gross {gross_cap:,.2f})",
                        raw,
                    ))
                continue
            approved_notional = min(requested, room)
            adjustments = [] if approved_notional == requested else [
                f"shrunk to max_short_exposure_pct ({requested:,.2f} -> {approved_notional:,.2f})"]

            per_name_cap = cfg.risk.max_position_pct * account.equity
            if approved_notional > per_name_cap:
                adjustments.append(f"shrunk to max_position_pct ({approved_notional:,.2f} -> {per_name_cap:,.2f})")
                approved_notional = per_name_cap

            # Shorts must be WHOLE shares on Alpaca — floor the quantity.
            qty = math.floor(approved_notional / clean["limit_price"])
            if qty < 1:
                result.rejected.append(
                    RejectedProposal(symbol, f"short notional {approved_notional:,.2f} rounds below 1 whole share", raw))
                continue
            approved_notional = math.floor(qty * clean["limit_price"] * 100) / 100
            stop_pct = stop_distance_pct(cfg, clean["limit_price"], data.atr14)
            stop_price = round(clean["limit_price"] * (1 + stop_pct), 4)   # stop ABOVE entry

            result.approved.append(
                ApprovedOrder(symbol=symbol, side="short", sleeve=clean["sleeve"],
                              notional=approved_notional, qty=float(qty),
                              limit_price=clean["limit_price"], stop_price=stop_price,
                              requested_notional=requested, adjustments=adjustments))
            committed_notional[f"SHORT:{symbol}"] = committed_notional.get(f"SHORT:{symbol}", 0.0) + approved_notional
            if held is None:
                open_position_count += 1
            continue

        # --- buys ---

        if result.new_entries_blocked:
            result.rejected.append(
                RejectedProposal(symbol, f"new entries blocked: {'; '.join(result.notes) or result.halt_reason}", raw)
            )
            continue

        data = ctx.symbols.get(symbol)
        if data is None:
            result.rejected.append(
                RejectedProposal(symbol, "no market data for symbol; cannot validate", raw)
            )
            continue

        # Universe filters
        if data.price < cfg.universe.min_price:
            result.rejected.append(
                RejectedProposal(symbol, f"price {data.price:.2f} below minimum {cfg.universe.min_price:.2f}", raw)
            )
            continue
        min_dollar_volume = cfg.universe.min_avg_dollar_volume
        if "tsmom" in clean["sleeve"]:
            min_dollar_volume = float(
                cfg.sleeves_paper.get(
                    "tsmom_min_dollar_volume", min_dollar_volume
                )
            )
        if data.avg_dollar_volume_20d < min_dollar_volume:
            result.rejected.append(
                RejectedProposal(
                    symbol,
                    f"20d avg dollar volume {data.avg_dollar_volume_20d:,.0f} below minimum "
                    f"{min_dollar_volume:,.0f}",
                    raw,
                )
            )
            continue
        if data.listed_days < cfg.universe.exclude_ipo_days:
            result.rejected.append(
                RejectedProposal(symbol, f"listed {data.listed_days}d < {cfg.universe.exclude_ipo_days}d IPO exclusion", raw)
            )
            continue

        # Averaging down — never, under any config.
        held = account.positions.get(symbol)
        if held is not None and held.unrealized_pct < 0:
            result.rejected.append(
                RejectedProposal(
                    symbol,
                    f"averaging down is never permitted (position is {held.unrealized_pct:.2%})",
                    raw,
                )
            )
            continue

        # Revenge-trade block
        loss_date = risk_state.recent_losses.get(symbol)
        if loss_date is not None:
            age = (ctx.now.date() - loss_date).days
            if age < cfg.risk.loss_reentry_block_days:
                result.rejected.append(
                    RejectedProposal(
                        symbol,
                        f"exited at a loss {age}d ago; re-entry blocked for "
                        f"{cfg.risk.loss_reentry_block_days}d",
                        raw,
                    )
                )
                continue

        # Position count
        if held is None and open_position_count >= cfg.risk.max_positions:
            result.rejected.append(
                RejectedProposal(symbol, f"already at max_positions ({cfg.risk.max_positions})", raw)
            )
            continue

        # Limit price must be within the slippage band of the reference price.
        slip = (clean["limit_price"] - data.price) / data.price if data.price > 0 else 0.0
        if slip > cfg.execution.max_limit_slippage_pct:
            result.rejected.append(
                RejectedProposal(
                    symbol,
                    f"limit price {clean['limit_price']:.4f} is {slip:.2%} through the touch, "
                    f"limit {cfg.execution.max_limit_slippage_pct:.2%}",
                    raw,
                )
            )
            continue

        approved_notional = requested

        # Max single position, counting anything already held.
        cap = cfg.risk.max_position_pct * account.equity
        existing = held.market_value if held else 0.0
        already_committed = committed_notional.get(symbol, 0.0)
        room = cap - existing - already_committed
        if room <= 0:
            result.rejected.append(
                RejectedProposal(
                    symbol,
                    f"position cap reached: {existing + already_committed:,.2f} already at/over "
                    f"{cfg.risk.max_position_pct:.0%} of equity ({cap:,.2f})",
                    raw,
                )
            )
            continue
        if approved_notional > room:
            adjustments.append(
                f"shrunk to max_position_pct {cfg.risk.max_position_pct:.0%} "
                f"({requested:,.2f} -> {room:,.2f})"
            )
            approved_notional = room

        # Portfolio-wide long and gross exposure. These are distinct from
        # buying headroom: shorts can release broker cash while still raising
        # gross risk, so broker buying power is never a sufficient risk cap.
        long_cap = cfg.risk.max_long_exposure_pct * account.equity
        gross_cap = cfg.risk.max_gross_exposure_pct * account.equity
        long_room = long_cap - long_exposure
        gross_room = gross_cap - gross_exposure
        exposure_room = min(long_room, gross_room)
        if exposure_room <= 0:
            result.rejected.append(
                RejectedProposal(
                    symbol,
                    f"long or gross exposure cap reached "
                    f"(long {long_cap:,.2f}, gross {gross_cap:,.2f})",
                    raw,
                )
            )
            continue
        if approved_notional > exposure_room:
            adjustments.append(
                f"shrunk to portfolio exposure cap "
                f"({approved_notional:,.2f} -> {exposure_room:,.2f})"
            )
            approved_notional = exposure_room

        # Leveraged-ETF exposure cap.
        if data.is_leveraged or symbol in cfg.leveraged_symbols:
            lev_cap = cfg.risk.max_leveraged_exposure_pct * account.equity
            lev_room = lev_cap - leveraged_exposure
            if lev_room <= 0:
                result.rejected.append(
                    RejectedProposal(
                        symbol,
                        f"leveraged exposure cap reached ({leveraged_exposure:,.2f} of "
                        f"{lev_cap:,.2f})",
                        raw,
                    )
                )
                continue
            if approved_notional > lev_room:
                adjustments.append(
                    f"shrunk to max_leveraged_exposure_pct {cfg.risk.max_leveraged_exposure_pct:.0%} "
                    f"({approved_notional:,.2f} -> {lev_room:,.2f})"
                )
                approved_notional = lev_room

        # Cash / margin headroom.
        if approved_notional > cash_remaining:
            if cash_remaining <= 0:
                result.rejected.append(RejectedProposal(symbol, "no buying headroom remaining", raw))
                continue
            adjustments.append(f"shrunk to available cash ({approved_notional:,.2f} -> {cash_remaining:,.2f})")
            approved_notional = cash_remaining

        qty = math.floor((approved_notional / clean["limit_price"]) * 1e6) / 1e6
        if qty <= 0:
            result.rejected.append(
                RejectedProposal(symbol, f"approved notional {approved_notional:,.2f} rounds to zero quantity", raw)
            )
            continue
        # Floor to cents, never round. round() can round *up*, which would
        # enlarge the order past what was requested and breach the gate's
        # contract by a fraction of a cent.
        approved_notional = math.floor(qty * clean["limit_price"] * 100) / 100

        stop_pct = stop_distance_pct(cfg, clean["limit_price"], data.atr14)
        stop_price = round(clean["limit_price"] * (1 - stop_pct), 4)
        if stop_price <= 0:
            result.rejected.append(RejectedProposal(symbol, "computed stop price is non-positive", raw))
            continue

        result.approved.append(
            ApprovedOrder(
                symbol=symbol,
                side="buy",
                sleeve=clean["sleeve"],
                notional=approved_notional,   # already floored to cents
                qty=qty,
                limit_price=clean["limit_price"],
                stop_price=stop_price,
                requested_notional=requested,
                adjustments=adjustments,
            )
        )

        committed_notional[symbol] = already_committed + approved_notional
        cash_remaining -= approved_notional
        long_exposure += approved_notional
        gross_exposure += approved_notional
        if held is None:
            open_position_count += 1
        if data.is_leveraged or symbol in cfg.leveraged_symbols:
            leveraged_exposure += approved_notional

    _assert_gate_invariants(result, cfg)
    return result


def _assert_gate_invariants(result: GateResult, cfg: Config) -> None:
    """The gate's contract, enforced at runtime as well as in tests.

    A bug that let the gate enlarge an order or drop a stop would be the single
    most expensive class of failure in this system, so it fails loudly here
    rather than reaching the broker.
    """
    for order in result.approved:
        if order.notional > order.requested_notional + 1e-6:
            raise AssertionError(
                f"GATE INVARIANT VIOLATED: {order.symbol} approved {order.notional} > "
                f"requested {order.requested_notional}"
            )
        if order.side == "buy":
            if order.stop_price <= 0:
                raise AssertionError(f"GATE INVARIANT VIOLATED: {order.symbol} buy has no stop")
            if order.stop_price >= order.limit_price:
                raise AssertionError(
                    f"GATE INVARIANT VIOLATED: {order.symbol} stop {order.stop_price} "
                    f">= entry {order.limit_price}"
                )
        if order.side == "short":
            if order.stop_price <= order.limit_price:
                raise AssertionError(
                    f"GATE INVARIANT VIOLATED: {order.symbol} short stop {order.stop_price} "
                    f"must be ABOVE entry {order.limit_price}"
                )
            if order.qty != int(order.qty):
                raise AssertionError(
                    f"GATE INVARIANT VIOLATED: {order.symbol} fractional short qty {order.qty}"
                )
        if order.side not in VALID_SIDES:
            raise AssertionError(f"GATE INVARIANT VIOLATED: {order.symbol} bad side {order.side!r}")
        if result.new_entries_blocked and order.side in ("buy", "short"):
            raise AssertionError(
                f"GATE INVARIANT VIOLATED: {order.symbol} {order.side} approved while entries are blocked"
            )
