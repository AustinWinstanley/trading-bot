"""Real (paper-broker) options order-submission daily job.

The 2x lab's first genuinely new order-submission code path beyond
equities — everything before this was read-only quote collection
(scripts/options_shadow.py and friends; tests/test_shadow_read_only.py
statically enforces that those stay that way).

Flow, in priority order (mirrors scripts/run_daily.py's own discipline —
safety checks before new risk):
  1. credential guard
  2. options-level pre-flight (every run, not just once — see
     scripts/check_options_level.py, which is the same check run by hand)
  3. reconciliation / assignment-detection (before anything touches the
     broker)
  4. fill/status reconciliation for pending journal rows
  5. exit pass (close-by-DTE or stand-down-forced)
  6. entry pass (only if reconciliation found no anomaly and no structure
     is already open for the experiment)

2x-lab only — base gets no options capability, hard-coded, not a config
default. Deliberately shares scripts.run_daily --profile 2x's flock lock
(scripts/paper.sh's `daily2x`, via the `options_daily2x` case) rather than
its own: both scripts read-modify-write the same state/risk_state_2x.json
keys (experiment_realized_pnl, experiment_standdowns) for this profile, and
an independent lock would let them race that file and submit to the same
paper account concurrently. Do not give this its own lock without
re-reading that reasoning.

    python -m scripts.options_daily --profile 2x [--dry-run]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sqlite3
import uuid
from zoneinfo import ZoneInfo

from engine.config import load_config
from engine.data import REPO_ROOT, load_env
from engine.execute import AlpacaError, OptionLeg, Trader, mirror_closing_legs
from engine.options_risk import (
    ApprovedOptionStructure,
    OptionLegQuote,
    OptionStructureProposal,
    evaluate_option_structure,
)
from engine.risk import (
    AccountState,
    MarketContext,
    RiskState,
    compute_experiment_standdowns,
)
from engine.risk import evaluate as evaluate_equity_gate
from scripts.check_options_level import MIN_LEVEL
from scripts.options_shadow import (
    _ensure_column,
    option_chain,
    select_delta_quote_pair,
    signal_state,
)

ET = ZoneInfo("America/New_York")
DB = REPO_ROOT / "state" / "options_2x.db"
RISK_STATE = REPO_ROOT / "state" / "risk_state_2x.json"
# The equity journal (scripts.run_daily --profile 2x's own database) —
# distinct from DB above. This module is 2x-only (see the --profile
# choices in main() below), so the path is deterministic; read-only here,
# opened mode=ro so a bug in this module can never write to the equity
# journal. Used by reconcile_option_structures's equity_explained_qty.
EQUITY_DB = REPO_ROOT / "state" / "paper_2x.db"

# The experiment-tier sleeve name (config_2x.yaml's experiments: block) —
# distinct from STRATEGY_KEY, the options_experiments config key shared
# with the read-only shadow (same structure-selection parameters, so the
# live structure and the shadow always select the identical shape).
EXPERIMENT_NAME = "bull_put_delta_selected_live"
STRATEGY_KEY = "bull_put_delta_selected"
OPEN_STATUSES = ("open_pending", "open", "closing_pending")
# Consecutive daily misses (pre-flight-caught or Alpaca-rejected) on
# insufficient options buying power before escalating to a CRITICAL log
# line — see main()'s entry pass. One miss is unremarkable (margin
# availability moves day to day with the 2x lab's leveraged core book);
# a run of these means the experiment has gone quiet and nobody would
# otherwise notice outside of reading this log by hand.
BUYING_POWER_MISS_ESCALATION = 3


def db() -> sqlite3.Connection:
    DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB)
    conn.executescript("""
    BEGIN;
    CREATE TABLE IF NOT EXISTS structures(
        structure_id TEXT PRIMARY KEY,
        experiment TEXT NOT NULL,
        strategy TEXT NOT NULL,
        underlying TEXT NOT NULL,
        expiration_date TEXT NOT NULL,
        contracts INTEGER NOT NULL,
        requested_contracts INTEGER NOT NULL,
        credit REAL NOT NULL,
        maximum_loss REAL NOT NULL,
        adjustments TEXT,
        opened_ts TEXT NOT NULL,
        open_client_order_id TEXT, open_alpaca_order_id TEXT,
        open_status TEXT, open_filled_at TEXT,
        close_reason TEXT,
        closed_ts TEXT,
        close_client_order_id TEXT, close_alpaca_order_id TEXT,
        close_status TEXT, close_filled_at TEXT,
        realized_pnl REAL,
        status TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS structure_legs(
        structure_id TEXT NOT NULL REFERENCES structures(structure_id),
        symbol TEXT NOT NULL, side TEXT NOT NULL, position_intent TEXT NOT NULL,
        ratio_qty INTEGER NOT NULL,
        quote_bid REAL, quote_ask REAL, quote_ts TEXT,
        PRIMARY KEY (structure_id, symbol)
    );
    CREATE TABLE IF NOT EXISTS reconciliation_events(
        ts TEXT NOT NULL, severity TEXT NOT NULL, structure_id TEXT, symbol TEXT, detail TEXT
    );
    """)
    # Additive migrations, same pattern as scripts/options_shadow.py's
    # _ensure_column — an already-deployed table gains new columns without
    # losing rows. Separated from the CREATE TABLE above specifically so
    # the very first schema change after initial deploy has a real,
    # already-proven path rather than needing one invented under pressure.
    for column, kind in (
        ("open_filled_avg_price", "REAL"),
        ("close_filled_avg_price", "REAL"),
    ):
        _ensure_column(conn, "structures", column, kind)
    return conn


# --------------------------------------------------------------------------
# Pure helpers — no I/O, unit-testable with synthetic fixtures.
# --------------------------------------------------------------------------


def reconcile_option_structures(
    positions: list[dict],
    open_structures: list[dict],
    *,
    equity_explained_qty: dict[str, float] | None = None,
) -> list[str]:
    """Do broker positions match the journal's open structures, and is
    there any unexplained equity position in an underlying with an open
    options structure (a possible early assignment)?

    ``positions`` is Trader.get_positions()'s raw list of dicts.
    ``open_structures`` is a list of
    {"structure_id", "underlying", "legs": [{"symbol", "position_intent"}, ...]}.
    ``equity_explained_qty`` is an optional {underlying: qty} map of how
    much of that symbol's broker equity position is already accounted for
    by this system's own equity orders (see
    ``equity_qty_explained_by_orders`` below) — an underlying that's also
    a core equity holding (SPY, in this repo's only live experiment)
    otherwise flags on *every* day a structure is open, since ordinary
    equity_core/trend SPY shares always coexist with an options structure
    trading the same name. Omitting it (or a symbol missing from it)
    treats the whole broker qty as unexplained, matching the original,
    stricter behavior — real assignment moves qty by a full contract's
    worth of shares (100, before any split), several orders of magnitude
    past the fractional-share rounding noise ordinary rebalancing leaves
    behind, so a wide tolerance here costs nothing in sensitivity.

    No automatic remediation lives here or anywhere in this module — this
    repo has never observed whether Alpaca's paper broker simulates early
    assignment at all or only settles at expiration, and encoding an
    unverified assumption into autonomous "smart" remediation is a worse
    risk than a loud, human-reviewed page.
    """
    equity_explained_qty = equity_explained_qty or {}
    findings: list[str] = []
    position_qty: dict[str, float] = {}
    position_asset_class: dict[str, str] = {}
    for p in positions:
        symbol = str(p.get("symbol", ""))
        try:
            qty = float(p.get("qty", 0))
        except (TypeError, ValueError):
            qty = 0.0
        position_qty[symbol] = qty
        position_asset_class[symbol] = str(p.get("asset_class", ""))

    underlyings_with_open_structures = {s["underlying"] for s in open_structures}
    for structure in open_structures:
        for leg in structure["legs"]:
            symbol = leg["symbol"]
            qty = position_qty.get(symbol)
            expect_short = leg["position_intent"] == "sell_to_open"
            if not qty:
                findings.append(
                    f"structure {structure['structure_id']} leg {symbol} is "
                    "missing from broker positions"
                )
            elif (qty < 0) != expect_short:
                findings.append(
                    f"structure {structure['structure_id']} leg {symbol} has "
                    f"unexpected sign (qty={qty}, expected "
                    f"{'short' if expect_short else 'long'})"
                )

    for symbol, asset_class in position_asset_class.items():
        if asset_class != "us_equity" or symbol not in underlyings_with_open_structures:
            continue
        actual_qty = position_qty.get(symbol, 0.0)
        explained_qty = equity_explained_qty.get(symbol, 0.0)
        unexplained_qty = actual_qty - explained_qty
        if abs(unexplained_qty) > 0.5:
            findings.append(
                f"unexplained {unexplained_qty:+.4f}-share equity position in "
                f"{symbol} while an options structure is open (broker qty "
                f"{actual_qty:.4f}, this system's own orders explain "
                f"{explained_qty:.4f}) — possible option assignment"
            )
    return findings


def equity_qty_explained_by_orders(conn: sqlite3.Connection, symbol: str) -> float:
    """Net quantity of ``symbol`` this system's own equity orders account
    for, from the EQUITY journal (``state/paper*.db`` — a different
    database than this module's own ``conn``/``state/options_2x.db``;
    callers must pass a connection to the right one). Buys/covers add,
    sells/shorts subtract. A genuine option assignment creates or removes
    broker shares directly, bypassing our order-submission path entirely,
    so it never appears here — the gap between this and the broker's
    actual quantity is exactly the signal
    ``reconcile_option_structures``'s ``equity_explained_qty`` needs.
    """
    row = conn.execute(
        "SELECT COALESCE(SUM(CASE "
        "WHEN side IN ('buy','cover') THEN filled_qty "
        "WHEN side IN ('sell','short') THEN -filled_qty "
        "ELSE 0 END), 0) FROM orders WHERE symbol=? AND status='filled'",
        (symbol,),
    ).fetchone()
    return float(row[0]) if row and row[0] is not None else 0.0


def load_equity_explained_qty(
    equity_db_path, open_structures: list[dict]
) -> dict[str, float]:
    """Build the {underlying: qty} map reconcile_option_structures's
    equity_explained_qty expects, reading equity_db_path (the profile's
    own state/paper*.db) read-only. Shared by scripts.options_daily's and
    scripts.healthcheck's call sites so the two never compute this two
    different ways.

    A missing journal, or one with no orders table yet (mirrors
    scripts.healthcheck.open_option_structures's handling of the
    equivalent case for the options journal), returns {} — the same
    conservative "nothing explained" default reconcile_option_structures
    already falls back to for an omitted or symbol-missing map, so a
    profile that's never run scripts.run_daily degrades to the original,
    stricter behavior rather than silently suppressing a real finding.
    """
    underlyings = {s["underlying"] for s in open_structures}
    if not underlyings or not equity_db_path.exists():
        return {}
    conn = sqlite3.connect(f"file:{equity_db_path}?mode=ro", uri=True)
    try:
        has_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='orders'"
        ).fetchone()
        if not has_table:
            return {}
        return {u: equity_qty_explained_by_orders(conn, u) for u in underlyings}
    finally:
        conn.close()


def _parse_quote_ts(raw: str | None, *, fallback: dt.datetime) -> dt.datetime:
    if not raw:
        return fallback
    text = str(raw).replace("Z", "+00:00")
    # Alpaca quotes carry nanosecond precision; datetime.fromisoformat only
    # accepts up to microseconds. Truncate the fractional-second field.
    if "." in text:
        head, rest = text.split(".", 1)
        frac, _, offset = rest.partition("+") if "+" in rest else rest.partition("-")
        sign = "+" if "+" in rest else ("-" if "-" in rest else "")
        text = f"{head}.{frac[:6]}{sign}{offset}" if sign else f"{head}.{frac[:6]}"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return fallback
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def build_proposal(pair: dict, *, sleeve: str, contracts: int, now: dt.datetime) -> OptionStructureProposal:
    """Turn scripts.options_shadow.select_delta_quote_pair's return dict
    into an OptionStructureProposal — reuses the shadow's own selection and
    economics, adds nothing new except the gate-facing shape."""
    legs = (
        OptionLegQuote(
            symbol=pair["short_symbol"], side="sell", position_intent="sell_to_open",
            ratio_qty=1, quote_ts=_parse_quote_ts(pair.get("short_quote_ts"), fallback=now),
            bid=float(pair["short_bid"]), ask=float(pair["short_ask"]),
        ),
        OptionLegQuote(
            symbol=pair["long_symbol"], side="buy", position_intent="buy_to_open",
            ratio_qty=1, quote_ts=_parse_quote_ts(pair.get("long_quote_ts"), fallback=now),
            bid=float(pair["long_bid"]), ask=float(pair["long_ask"]),
        ),
    )
    return OptionStructureProposal(
        sleeve=sleeve,
        underlying="SPY",
        expiration_date=dt.date.fromisoformat(pair["expiration_date"]),
        legs=legs,
        contracts=contracts,
        credit=float(pair["executable_credit"]),
        maximum_loss=float(pair["maximum_loss"]),
    )


# --------------------------------------------------------------------------
# Journal access
# --------------------------------------------------------------------------


def fetch_open_structures(conn: sqlite3.Connection, experiment: str) -> list[dict]:
    rows = conn.execute(
        "SELECT structure_id, underlying, expiration_date, contracts, credit, "
        "maximum_loss, status, open_alpaca_order_id, close_alpaca_order_id, "
        "open_filled_avg_price FROM structures WHERE experiment=? AND status IN "
        f"({','.join('?' * len(OPEN_STATUSES))})",
        (experiment, *OPEN_STATUSES),
    ).fetchall()
    out = []
    for row in rows:
        (structure_id, underlying, expiration_date, contracts, credit,
         maximum_loss, status, open_id, close_id, open_fill_px) = row
        legs = conn.execute(
            "SELECT symbol, side, position_intent, ratio_qty FROM structure_legs "
            "WHERE structure_id=?", (structure_id,)
        ).fetchall()
        out.append({
            "structure_id": structure_id, "underlying": underlying,
            "expiration_date": dt.date.fromisoformat(expiration_date),
            "contracts": contracts, "credit": credit, "maximum_loss": maximum_loss,
            "status": status, "open_alpaca_order_id": open_id,
            "close_alpaca_order_id": close_id, "open_filled_avg_price": open_fill_px,
            "legs": [
                {"symbol": s, "side": sd, "position_intent": pi, "ratio_qty": rq}
                for (s, sd, pi, rq) in legs
            ],
        })
    return out


def insert_structure(
    conn: sqlite3.Connection,
    approved: ApprovedOptionStructure,
    proposal: OptionStructureProposal,
    *,
    structure_id: str,
    ts: str,
    order: dict,
) -> None:
    conn.execute(
        "INSERT INTO structures(structure_id, experiment, strategy, underlying, "
        "expiration_date, contracts, requested_contracts, credit, maximum_loss, "
        "adjustments, opened_ts, open_client_order_id, open_alpaca_order_id, "
        "open_status, status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (structure_id, EXPERIMENT_NAME, STRATEGY_KEY, approved.underlying,
         approved.expiration_date.isoformat(), approved.contracts,
         approved.requested_contracts, approved.credit, approved.maximum_loss,
         json.dumps(approved.adjustments), ts, order.get("client_order_id"),
         order.get("id"), order.get("status"), "open_pending"),
    )
    quotes = {leg.symbol: leg for leg in proposal.legs}
    for leg in approved.legs:
        quote = quotes.get(leg.symbol)
        conn.execute(
            "INSERT INTO structure_legs(structure_id, symbol, side, position_intent, "
            "ratio_qty, quote_bid, quote_ask, quote_ts) VALUES (?,?,?,?,?,?,?,?)",
            (structure_id, leg.symbol, leg.side, leg.position_intent, leg.ratio_qty,
             quote.bid if quote else None, quote.ask if quote else None,
             quote.quote_ts.isoformat() if quote else None),
        )


def record_close_submission(conn: sqlite3.Connection, structure_id: str, *, order: dict, reason: str) -> None:
    conn.execute(
        "UPDATE structures SET status='closing_pending', close_reason=?, "
        "close_client_order_id=?, close_alpaca_order_id=?, close_status=? "
        "WHERE structure_id=?",
        (reason, order.get("client_order_id"), order.get("id"), order.get("status"),
         structure_id),
    )


def log_reconciliation_event(conn: sqlite3.Connection, ts: str, detail: str, *, structure_id: str | None = None) -> None:
    conn.execute(
        "INSERT INTO reconciliation_events(ts, severity, structure_id, symbol, detail) "
        "VALUES (?,?,?,?,?)",
        (ts, "CRITICAL", structure_id, None, detail),
    )


def reconcile_pending_orders(conn: sqlite3.Connection, trader: Trader, now: dt.datetime) -> None:
    """Poll Alpaca for any structure still waiting on an open/close fill and
    update the journal — mirrors scripts/run_daily.py's
    reconcile_journal_orders for the equity path."""
    rows = conn.execute(
        "SELECT structure_id, status, open_alpaca_order_id, close_alpaca_order_id, "
        "contracts, open_filled_avg_price FROM structures WHERE status IN "
        "('open_pending','closing_pending')"
    ).fetchall()
    for structure_id, status, open_id, close_id, contracts, open_fill_px in rows:
        if status == "open_pending" and open_id:
            try:
                remote = trader.get_order(open_id)
            except Exception:
                continue
            if str(remote.get("status")) != "filled":
                continue
            fill_px = float(remote["filled_avg_price"]) if remote.get("filled_avg_price") else None
            conn.execute(
                "UPDATE structures SET status='open', open_status=?, "
                "open_filled_at=?, open_filled_avg_price=? WHERE structure_id=?",
                (remote.get("status"), remote.get("filled_at"), fill_px, structure_id),
            )
        elif status == "closing_pending" and close_id:
            try:
                remote = trader.get_order(close_id)
            except Exception:
                continue
            if str(remote.get("status")) != "filled":
                continue
            close_fill_px = float(remote["filled_avg_price"]) if remote.get("filled_avg_price") else None
            realized_pnl = None
            if close_fill_px is not None and open_fill_px is not None:
                # Alpaca's own sign convention (positive=debit paid,
                # negative=credit received) at both legs of the round trip;
                # net cash received = -(open + close) per contract, x100
                # for the option multiplier, x contracts for the structure.
                realized_pnl = -(open_fill_px + close_fill_px) * 100 * contracts
            conn.execute(
                "UPDATE structures SET status='closed', close_status=?, "
                "close_filled_at=?, close_filled_avg_price=?, closed_ts=?, "
                "realized_pnl=? WHERE structure_id=?",
                (remote.get("status"), remote.get("filled_at"), close_fill_px,
                 now.isoformat(), realized_pnl, structure_id),
            )


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def _buying_power_shortfall(options_buying_power: float | None, required: float) -> bool:
    """True if the account's real options buying power is insufficient
    for the required margin estimate. None (field absent from the
    account payload) means "unknown" — defers to Alpaca's own rejection
    rather than blocking on a guess."""
    return options_buying_power is not None and options_buying_power < required


def _buying_power_miss_message(buying_power_misses: dict, detail: str) -> str:
    """Increments EXPERIMENT_NAME's consecutive-miss counter in place and
    returns the line to print for it — plain at first, escalating to
    CRITICAL (picked up by scripts/weekly.py's log scrape, same mechanism
    every other CRITICAL in this codebase relies on — no separate alert
    channel) once BUYING_POWER_MISS_ESCALATION consecutive misses
    accumulate. A single miss is unremarkable — margin availability moves
    day to day with the 2x lab's leveraged core book — a run of them
    means the experiment has gone quiet with nothing else surfacing it."""
    misses = buying_power_misses.get(EXPERIMENT_NAME, 0) + 1
    buying_power_misses[EXPERIMENT_NAME] = misses
    if misses >= BUYING_POWER_MISS_ESCALATION:
        return (
            f"CRITICAL: options entry stalled on insufficient buying power "
            f"for {misses} consecutive day(s) — {detail}"
        )
    return f"    {detail} (buying-power miss {misses}/{BUYING_POWER_MISS_ESCALATION})"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=("2x",), default="2x")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    cfg = load_config(REPO_ROOT / "config_2x.yaml")
    load_env()
    key = os.environ.get("ALPACA_API_KEY_2X")
    secret = os.environ.get("ALPACA_API_SECRET_2X")
    if not (key and secret):
        print("CRITICAL: profile 2x needs ALPACA_API_KEY_2X / ALPACA_API_SECRET_2X "
              "in .env — standing down")
        raise SystemExit(1)
    trader = Trader(key=key, secret=secret, max_calls_per_min=40)

    now = dt.datetime.now(ET)
    today = now.date()
    clock = trader.clock()
    is_trading_day = bool(clock.get("is_open")) or args.dry_run

    account = trader.get_account()
    level = account.get("options_trading_level")
    if level is None:
        level = account.get("options_approved_level")
    if level is None or int(level) < MIN_LEVEL:
        print(f"CRITICAL: options level {level!r} is below the required "
              f"Level {MIN_LEVEL} — standing down, no orders submitted")
        raise SystemExit(1)
    # No second API call: this is the same account object already fetched
    # for the level check above. None (field absent) means "unknown" —
    # the pre-flight check below skips rather than guessing, and Alpaca's
    # own rejection remains the backstop either way.
    raw_buying_power = account.get("options_buying_power")
    options_buying_power = float(raw_buying_power) if raw_buying_power is not None else None

    conn = db()
    experiment = cfg.experiments.get(EXPERIMENT_NAME)
    if experiment is None:
        print(f"experiment {EXPERIMENT_NAME!r} not registered in config_2x.yaml — nothing to do")
        if not args.dry_run:
            conn.commit()
        return

    open_structures = fetch_open_structures(conn, EXPERIMENT_NAME)
    positions = trader.get_positions()
    equity_explained_qty = load_equity_explained_qty(EQUITY_DB, open_structures)
    findings = reconcile_option_structures(
        positions, open_structures, equity_explained_qty=equity_explained_qty
    )
    for finding in findings:
        print(f"CRITICAL: {finding}")
        log_reconciliation_event(conn, now.isoformat(), finding)
    anomaly = bool(findings)

    reconcile_pending_orders(conn, trader, now)
    open_structures = fetch_open_structures(conn, EXPERIMENT_NAME)

    equity = float(account["equity"])
    st = json.loads(RISK_STATE.read_text()) if RISK_STATE.exists() else {}
    buying_power_misses = dict(st.get("experiment_buying_power_misses", {}))
    exp_realized = dict(st.get("experiment_realized_pnl", {}))
    # Refresh MY experiment's realized P&L directly from the journal (the
    # authoritative source) so a fill that just reconciled above is
    # reflected in this same run's stand-down decision, not only the next
    # one. Every OTHER experiment's persisted value (e.g. an equity
    # experiment scripts.run_daily --profile 2x owns) is left exactly as
    # read — this script has no visibility into their P&L and must never
    # clobber it.
    closed_rows = conn.execute(
        "SELECT realized_pnl FROM structures WHERE experiment=? AND status='closed' "
        "AND realized_pnl IS NOT NULL",
        (EXPERIMENT_NAME,),
    ).fetchall()
    exp_realized[EXPERIMENT_NAME] = sum(r[0] for r in closed_rows)

    # Covers every registered experiment (mirroring scripts.run_daily
    # --profile 2x's own computation) using each one's persisted realized
    # P&L; unrealized is only ever populated for MY experiment below (0 for
    # any other type this script cannot see) — a conservative gap, not a
    # clobbering one, so the final overwrite of experiment_standdowns stays
    # a safe full recompute instead of needing a separate merge step.
    cumulative_pnl_pct = {
        name: exp_realized.get(name, 0.0) / equity for name in cfg.experiments
    } if equity > 0 else {}
    experiment_standdowns = compute_experiment_standdowns(cfg, cumulative_pnl_pct)
    risk_state = RiskState(
        peak_equity=st.get("peak_equity", equity),
        day_start_equity=st.get("day_start_equity", equity),
        month_start_equity=st.get("month_start_equity", equity),
        recent_losses={}, halted=bool(st.get("halted", False)),
        experiment_standdowns=experiment_standdowns,
    )

    equity_gate_result = evaluate_equity_gate(
        [], AccountState(equity=equity, cash=float(account["cash"])),
        risk_state, MarketContext(now=now, is_trading_day=is_trading_day), cfg,
    )
    new_entries_blocked = equity_gate_result.new_entries_blocked or anomaly

    account_state = AccountState(
        equity=equity, cash=float(account["cash"]),
        experiment_gross_exposure={
            EXPERIMENT_NAME: sum(
                s["maximum_loss"] * s["contracts"] for s in open_structures
                if s["status"] in ("open_pending", "open")
            )
        },
    )

    # ---- exit pass: close-by-DTE, or the experiment just stood down -----
    close_by_dte = int(
        cfg.sleeves_paper["options_experiments"][STRATEGY_KEY]["close_by_dte_trading_days"]
    )
    for structure in open_structures:
        if structure["status"] != "open":
            continue
        days_to_expiry = (structure["expiration_date"] - today).days
        should_close = (
            days_to_expiry <= close_by_dte or EXPERIMENT_NAME in experiment_standdowns
        )
        if not should_close:
            continue
        reason = "standdown_flatten" if EXPERIMENT_NAME in experiment_standdowns else "close_by_dte"
        legs = tuple(
            OptionLeg(leg["symbol"], leg["side"], leg["position_intent"], leg["ratio_qty"])
            for leg in structure["legs"]
        )
        closing_legs = mirror_closing_legs(legs)
        print(f"  CLOSE {structure['structure_id']} ({reason}), {days_to_expiry}d to expiry")
        if args.dry_run:
            continue
        try:
            order = trader.submit_multi_leg_order(
                closing_legs, qty=structure["contracts"],
                credit=-structure["credit"],  # closing a credit spread is a debit, approximately
                client_order_id=f"opt-{today:%Y%m%d}-{structure['structure_id'][:8]}-close",
            )
            record_close_submission(conn, structure["structure_id"], order=order, reason=reason)
        except AlpacaError as exc:
            print(f"    close submit FAILED: {exc}")

    # ---- entry pass -------------------------------------------------------
    open_count = sum(
        1 for s in fetch_open_structures(conn, EXPERIMENT_NAME)
        if s["status"] in ("open_pending", "open", "closing_pending")
    )
    if anomaly:
        print("  entry pass skipped: reconciliation anomaly found this run")
    elif new_entries_blocked:
        print("  entry pass skipped: new entries blocked")
    else:
        strategy_cfg = cfg.sleeves_paper["options_experiments"][STRATEGY_KEY]
        if strategy_cfg.get("mode") not in ("shadow", "paper"):
            print(f"  entry pass skipped: options_experiments.{STRATEGY_KEY}.mode is off")
        else:
            try:
                signal = signal_state(trader, today)
                spot = trader.latest_price("SPY")
                if not spot:
                    raise ValueError("SPY latest price unavailable")
                snapshots = option_chain(trader, spot, today)
                pair = select_delta_quote_pair(
                    snapshots, today=today,
                    target_dte=int(strategy_cfg["target_dte"]),
                    target_delta=float(strategy_cfg["target_short_delta"]),
                    width=float(strategy_cfg["strike_width"]),
                )
                proposal = build_proposal(
                    pair, sleeve=EXPERIMENT_NAME, contracts=1, now=now,
                )
                if not signal["signal_enabled"]:
                    print("  entry pass: signal not enabled, no new structure")
                else:
                    result = evaluate_option_structure(
                        proposal, account_state, risk_state, cfg,
                        now=now, new_entries_blocked=new_entries_blocked,
                        open_structure_count=open_count,
                    )
                    for rej in result.rejected:
                        print(f"  REJECT {rej.underlying}: {rej.reason}")
                    for approved in result.approved:
                        # Pre-flight: same account object already fetched
                        # for the options-level check above, no extra API
                        # call. required_bp is our own risk model's
                        # maximum_loss, not Alpaca's exact margin formula
                        # (observed ~10% higher than Alpaca's real
                        # cost_basis in one case) — that's the safe
                        # direction to be imprecise in: reject a
                        # borderline case here rather than let Alpaca's
                        # 403 do it after the order already left.
                        required_bp = approved.maximum_loss * approved.contracts
                        if _buying_power_shortfall(options_buying_power, required_bp):
                            print(_buying_power_miss_message(
                                buying_power_misses,
                                f"REJECT {approved.underlying}: "
                                f"options_buying_power ${options_buying_power:,.2f} "
                                f"< required ${required_bp:,.2f}",
                            ))
                            continue
                        print(
                            f"  APPROVED open {approved.underlying} "
                            f"{approved.contracts} contract(s) credit=${approved.credit:.2f} "
                            f"max_loss=${approved.maximum_loss:.2f} {approved.adjustments}"
                        )
                        if args.dry_run:
                            continue
                        structure_id = uuid.uuid4().hex
                        try:
                            order = trader.submit_multi_leg_order(
                                approved.legs, qty=approved.contracts,
                                credit=approved.credit,
                                client_order_id=f"opt-{today:%Y%m%d}-{structure_id[:8]}-open",
                            )
                            insert_structure(
                                conn, approved, proposal,
                                structure_id=structure_id, ts=now.isoformat(), order=order,
                            )
                            buying_power_misses[EXPERIMENT_NAME] = 0
                        except AlpacaError as exc:
                            if "buying power" in str(exc).lower():
                                print(_buying_power_miss_message(
                                    buying_power_misses, f"open submit FAILED: {exc}",
                                ))
                            else:
                                print(f"    open submit FAILED: {exc}")
            except ValueError as exc:
                print(f"  entry pass: no qualifying structure ({exc})")

    if args.dry_run:
        conn.rollback()
        print("done: dry run; no orders or local state changes")
        return

    # A closing order may have filled between the reconciliation pass above
    # and here only if this run's own exit-pass close just got a same-tick
    # fill, which Alpaca's paper broker will not report synchronously — so
    # exp_realized/experiment_standdowns computed above already reflect the
    # authoritative journal state for this run. Persist them as-is.
    st["experiment_realized_pnl"] = exp_realized
    st["experiment_standdowns"] = sorted(experiment_standdowns)
    st["experiment_buying_power_misses"] = buying_power_misses
    RISK_STATE.write_text(json.dumps(st, indent=2))
    conn.commit()
    print("done")


if __name__ == "__main__":
    main()
