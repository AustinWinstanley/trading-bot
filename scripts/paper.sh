#!/usr/bin/env bash
# Cron wrapper for the paper loop.
#  - flock so a slow run can never overlap the next
#  - dated logs
#  - a non-zero exit writes a CRITICAL line the weekly report surfaces
set -u
BOT=/home/austin/trading-bot
LOG_DIR=$BOT/logs
mkdir -p "$LOG_DIR"
LOG=$LOG_DIR/paper-$(date -u +%Y%m%d).log
JOB=${1:-daily}

{
  echo "=== $(date -u '+%F %T UTC') job=$JOB ==="
  flock -n 200 || { echo "CRITICAL: previous run still holds the lock"; exit 1; }
  cd "$BOT"
  case "$JOB" in
    daily)    timeout 1500 .venv/bin/python -m scripts.run_daily ;;
    daily2x)  timeout 1500 .venv/bin/python -m scripts.run_daily --profile 2x ;;
    weekly)   timeout 3000 .venv/bin/python -m scripts.weekly ;;
    health)   timeout 120 .venv/bin/python -m scripts.healthcheck ;;
    health2x) timeout 120 .venv/bin/python -m scripts.healthcheck --profile 2x ;;
    *) echo "unknown job $JOB"; exit 2 ;;
  esac
  rc=$?
  [ $rc -ne 0 ] && echo "CRITICAL: job=$JOB exited rc=$rc"
  echo "=== end rc=$rc ==="
  exit $rc
} 200>"$BOT/state/paper-$JOB.lock" >>"$LOG" 2>&1
