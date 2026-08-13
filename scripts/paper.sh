#!/usr/bin/env bash
# Cron wrapper for the paper loop.
#  - flock so a slow run can never overlap the next
#  - dated logs
#  - a non-zero exit writes a CRITICAL line the weekly report surfaces
#  - optional ET slot guard (see below)
#
# The server clock is UTC and Debian cron has no CRON_TZ, so an ET schedule
# needs two UTC lines per job — one that lands correctly under EDT and one
# under EST. Both fire year-round, which ran every job twice a day and let the
# off-DST copy trade an hour late. Passing the intended ET time as $2 makes the
# wrong-half-of-the-year copy exit as a no-op, so exactly one runs whatever the
# offset. Omit $2 to run unconditionally.
set -u
# Production keeps the fixed server path. This is a mutex, not a convenience:
# the lock file lives at $BOT/state/paper-$LOCK.lock, so if two checkouts
# resolved different $BOT values they would hold independent locks and could
# run scripts.run_daily concurrently against the SAME live Alpaca account —
# a real correctness hazard, not just an inconvenience. Tests and one-off
# validation may still point the wrapper at an isolated checkout without
# writing locks under /home/austin; cron never sets this override.
BOT=${PAPER_BOT_ROOT:-/home/austin/trading-bot}
# Overridable so tests can exercise the slot guard without writing into the
# production log the weekly report scrapes.
LOG_DIR=${PAPER_LOG_DIR:-$BOT/logs}
mkdir -p "$LOG_DIR"
LOG=$LOG_DIR/paper-$(date -u +%Y%m%d).log
JOB=${1:-daily}
SLOT=${2:-}

# Minutes since ET midnight, for the slot comparison.
et_minutes() { local h=${1%%:*} m=${1##*:}; echo $((10#$h * 60 + 10#$m)); }

if [ -n "$SLOT" ]; then
  now_et=$(TZ=America/New_York date '+%H:%M')
  delta=$(( $(et_minutes "$now_et") - $(et_minutes "$SLOT") ))
  [ $delta -lt 0 ] && delta=$(( -delta ))
  # The two candidate firings are 60 min apart; a 5 min window separates them
  # unambiguously while tolerating cron lag.
  if [ $delta -gt 5 ]; then
    echo "=== $(date -u '+%F %T UTC') job=$JOB skip: ET $now_et != slot $SLOT ===" >>"$LOG"
    exit 0
  fi
fi

# LOCK is resolved from $JOB before the flock block opens below, since bash
# fixes the 200> target for the whole compound command up front. stops/
# stops2x deliberately share daily's/daily2x's lock — they must never run
# concurrently with a full rebalance against the same SQLite journal, and a
# stops check that loses the race just waits for the next cron minute; the
# full run checks stops itself in the same cycle, so nothing is missed.
case "$JOB" in
  daily)     LOCK=daily;     TIMEOUT=1500 ;;
  daily2x)   LOCK=daily2x;   TIMEOUT=1500 ;;
  stops)     LOCK=daily;     TIMEOUT=120  ;;
  stops2x)   LOCK=daily2x;   TIMEOUT=120  ;;
  # Deliberately shares daily2x's lock, NOT its own (unlike shadows2x
  # below): this job WRITES real orders and read-modify-writes
  # state/risk_state_2x.json's experiment_realized_pnl/experiment_standdowns
  # keys — the exact same file scripts.run_daily --profile 2x also owns.
  # An independent lock would let both race that file and submit orders to
  # the same paper account at the same instant. shadows2x earned its own
  # lock BECAUSE it is read-only; this job is the opposite case. See
  # scripts/options_daily.py's module docstring for the same reasoning.
  options_daily2x) LOCK=daily2x; TIMEOUT=300 ;;
  # Own lock, deliberately NOT shared with daily2x: these are read-only
  # research collectors, not trading. Sharing daily2x's lock (as they did
  # until 2026-08-12) meant a slow quote fetch extended how long a stops2x
  # check could be starved by flock -n — research latency was borrowing
  # against a live risk control's responsiveness. 600s covers the four
  # scripts' own timeouts (120+180+120+120=540) plus slack.
  shadows2x) LOCK=shadows2x; TIMEOUT=600  ;;
  weekly)    LOCK=weekly;    TIMEOUT=3000 ;;
  # Mid-week 2x-only MOM_LS rebuild — reports/mom_ls_cadence_study.json's
  # experiment-tier trial (config.yaml/base stays weekly-only). Deliberately
  # shares weekly's lock: both write state/mom_ls_targets_2x.json and must
  # never race each other.
  momls2x)   LOCK=weekly;    TIMEOUT=900  ;;
  health)    LOCK=health;    TIMEOUT=120  ;;
  health2x)  LOCK=health2x;  TIMEOUT=120  ;;
  *) LOCK=$JOB; TIMEOUT=120 ;;   # unknown job still needs a lock name; caught below
esac

{
  echo "=== $(date -u '+%F %T UTC') job=$JOB slot=${SLOT:-any} ==="
  flock -n 200 || { echo "CRITICAL: previous run still holds the lock"; exit 1; }
  cd "$BOT"
  case "$JOB" in
    daily)    timeout "$TIMEOUT" .venv/bin/python -m scripts.run_daily ;;
    daily2x)  timeout "$TIMEOUT" .venv/bin/python -m scripts.run_daily --profile 2x ;;
    stops)    timeout "$TIMEOUT" .venv/bin/python -m scripts.run_daily --stops-only ;;
    stops2x)  timeout "$TIMEOUT" .venv/bin/python -m scripts.run_daily --stops-only --profile 2x ;;
    options_daily2x) timeout "$TIMEOUT" .venv/bin/python -m scripts.options_daily --profile 2x ;;
    # Read-only research collectors. Runs independently of daily2x (own lock,
    # own schedule slot) so a hung/slow quote fetch here can never delay or
    # block a trading run or a stops2x check. Each script's own failure is
    # CRITICAL (not a soft WARNING) so it surfaces in the weekly report scrape
    # — a silently-dead collector should not read as healthy.
    shadows2x)
      shadow_failures=0
      timeout 120 .venv/bin/python -m scripts.options_shadow --profile 2x ||
        { echo "CRITICAL: read-only options shadow failed"; shadow_failures=$((shadow_failures + 1)); }
      timeout 180 .venv/bin/python -m scripts.momentum_options_shadow --profile 2x ||
        { echo "CRITICAL: read-only momentum-options shadow failed"; shadow_failures=$((shadow_failures + 1)); }
      timeout 120 .venv/bin/python -m scripts.event_volatility_shadow --profile 2x ||
        { echo "CRITICAL: read-only event-volatility shadow failed"; shadow_failures=$((shadow_failures + 1)); }
      timeout 120 .venv/bin/python -m scripts.zero_dte_shadow --profile 2x ||
        { echo "CRITICAL: read-only 0DTE shadow failed"; shadow_failures=$((shadow_failures + 1)); }
      [ "$shadow_failures" -eq 0 ]
      ;;
    weekly)   timeout "$TIMEOUT" .venv/bin/python -m scripts.weekly ;;
    momls2x)  timeout "$TIMEOUT" .venv/bin/python -m scripts.weekly --mom-ls-only ;;
    health)   timeout "$TIMEOUT" .venv/bin/python -m scripts.healthcheck ;;
    health2x) timeout "$TIMEOUT" .venv/bin/python -m scripts.healthcheck --profile 2x ;;
    *) echo "unknown job $JOB"; exit 2 ;;
  esac
  rc=$?
  [ $rc -ne 0 ] && echo "CRITICAL: job=$JOB exited rc=$rc"
  echo "=== end rc=$rc ==="
  exit $rc
} 200>"$BOT/state/paper-$LOCK.lock" >>"$LOG" 2>&1
