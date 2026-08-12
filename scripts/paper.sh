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
# Production keeps the fixed server path. Tests and one-off validation may
# point the wrapper at an isolated checkout without writing locks under
# /home/user; cron never sets this override.
BOT=${PAPER_BOT_ROOT:-/home/user/trading-bot}
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
  daily)    LOCK=daily;    TIMEOUT=1500 ;;
  daily2x)  LOCK=daily2x;  TIMEOUT=1500 ;;
  stops)    LOCK=daily;    TIMEOUT=120  ;;
  stops2x)  LOCK=daily2x;  TIMEOUT=120  ;;
  weekly)   LOCK=weekly;   TIMEOUT=3000 ;;
  health)   LOCK=health;   TIMEOUT=120  ;;
  health2x) LOCK=health2x; TIMEOUT=120  ;;
  *) LOCK=$JOB; TIMEOUT=120 ;;   # unknown job still needs a lock name; caught below
esac

{
  echo "=== $(date -u '+%F %T UTC') job=$JOB slot=${SLOT:-any} ==="
  flock -n 200 || { echo "CRITICAL: previous run still holds the lock"; exit 1; }
  cd "$BOT"
  case "$JOB" in
    daily)    timeout "$TIMEOUT" .venv/bin/python -m scripts.run_daily ;;
    daily2x)
      timeout "$TIMEOUT" .venv/bin/python -m scripts.run_daily --profile 2x &&
        timeout 120 .venv/bin/python -m scripts.options_shadow --profile 2x
      ;;
    stops)    timeout "$TIMEOUT" .venv/bin/python -m scripts.run_daily --stops-only ;;
    stops2x)  timeout "$TIMEOUT" .venv/bin/python -m scripts.run_daily --stops-only --profile 2x ;;
    weekly)   timeout "$TIMEOUT" .venv/bin/python -m scripts.weekly ;;
    health)   timeout "$TIMEOUT" .venv/bin/python -m scripts.healthcheck ;;
    health2x) timeout "$TIMEOUT" .venv/bin/python -m scripts.healthcheck --profile 2x ;;
    *) echo "unknown job $JOB"; exit 2 ;;
  esac
  rc=$?
  [ $rc -ne 0 ] && echo "CRITICAL: job=$JOB exited rc=$rc"
  echo "=== end rc=$rc ==="
  exit $rc
} 200>"$BOT/state/paper-$LOCK.lock" >>"$LOG" 2>&1
