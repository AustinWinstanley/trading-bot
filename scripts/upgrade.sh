#!/usr/bin/env bash
# Fail-closed server upgrade verification.
#
# The script runs itself from a temporary copy, pauses only crontab lines that
# invoke this project's paper wrapper, stashes tracked runtime reports, pulls
# main, verifies the checkout against both Alpaca paper profiles, and restores
# the exact original crontab only after every check passes.
set -Eeuo pipefail

if [[ "${TRADING_BOT_UPGRADE_BOOTSTRAPPED:-0}" != "1" ]]; then
  bootstrap_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
  bootstrap_tmp="$(mktemp -d "${TMPDIR:-/tmp}/trading-bot-upgrade.XXXXXX")"
  cp "${BASH_SOURCE[0]}" "$bootstrap_tmp/upgrade.sh"
  chmod 700 "$bootstrap_tmp/upgrade.sh"
  export TRADING_BOT_UPGRADE_BOOTSTRAPPED=1
  export TRADING_BOT_UPGRADE_REPO_ROOT="$bootstrap_root"
  export TRADING_BOT_UPGRADE_TEMP_DIR="$bootstrap_tmp"
  exec bash "$bootstrap_tmp/upgrade.sh" "$@"
fi

REPO_ROOT="${TRADING_BOT_UPGRADE_REPO_ROOT:?missing upgrade repository root}"
UPGRADE_TEMP_DIR="${TRADING_BOT_UPGRADE_TEMP_DIR:-}"
PYTHON="$REPO_ROOT/.venv/bin/python"
PIP="$REPO_ROOT/.venv/bin/pip"
STATE_DIR="$REPO_ROOT/state"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
CRON_BACKUP="$STATE_DIR/crontab-before-upgrade-$STAMP.txt"
CRON_PAUSED_FILE="$STATE_DIR/crontab-paused-$STAMP.txt"
PAUSE_MARKER="# TRADING-BOT-UPGRADE-PAUSED "
CRON_WAS_PRESENT=0
CRON_IS_PAUSED=0
REPORT_STASHED=0
REPORT_STASH_REF=""
REPORT_STASH_NEEDS_MANUAL=0
UPGRADE_SUCCEEDED=0

cleanup_temp_copy() {
  if [[ -n "$UPGRADE_TEMP_DIR" && -d "$UPGRADE_TEMP_DIR" ]]; then
    rm -f "$UPGRADE_TEMP_DIR/upgrade.sh"
    rmdir "$UPGRADE_TEMP_DIR" 2>/dev/null || true
  fi
}

restore_report_stash() {
  if [[ "$REPORT_STASHED" -ne 1 ]]; then
    return 0
  fi

  echo "==> Restoring server-generated paper reports"
  # Mark this attempt consumed before popping so the EXIT trap never retries a
  # conflicted pop. Git retains the stash when pop cannot apply cleanly.
  REPORT_STASHED=0
  if git stash pop --quiet "$REPORT_STASH_REF"; then
    REPORT_STASH_REF=""
    return 0
  fi

  REPORT_STASH_NEEDS_MANUAL=1
  echo "Paper-report stash could not be restored automatically." >&2
  echo "Git retained it as $REPORT_STASH_REF; inspect 'git status' before resolving." >&2
  return 1
}

on_exit() {
  rc=$?
  if [[ "$UPGRADE_SUCCEEDED" -eq 1 ]]; then
    cleanup_temp_copy
    exit "$rc"
  fi
  if [[ "$REPORT_STASHED" -eq 1 ]]; then
    set +e
    restore_report_stash
    set -e
  fi
  echo
  echo "UPGRADE FAILED (exit $rc)."
  if [[ "$CRON_IS_PAUSED" -eq 1 ]]; then
    echo "Trading-bot cron jobs remain PAUSED."
    if [[ "$CRON_WAS_PRESENT" -eq 1 ]]; then
      echo "After resolving the failure, restore the original crontab with:"
      echo "  crontab '$CRON_BACKUP'"
    else
      echo "There was no original user crontab to restore."
    fi
  fi
  if [[ "$REPORT_STASH_NEEDS_MANUAL" -eq 1 ]]; then
    echo "The paper-report stash remains available as $REPORT_STASH_REF."
  fi
  echo "No orders were submitted by this upgrade script."
  cleanup_temp_copy
  exit "$rc"
}
trap on_exit EXIT

if [[ "${1:-}" == "--restore" ]]; then
  if [[ $# -ne 2 || ! -f "$2" ]]; then
    echo "usage: $0 --restore /path/to/crontab-backup.txt" >&2
    exit 2
  fi
  crontab "$2"
  echo "Restored crontab from $2"
  UPGRADE_SUCCEEDED=1
  exit 0
fi
if [[ $# -ne 0 ]]; then
  echo "usage: $0 [--restore /path/to/crontab-backup.txt]" >&2
  exit 2
fi

mkdir -p "$STATE_DIR"
cd "$REPO_ROOT"

if [[ ! -x "$PYTHON" || ! -x "$PIP" ]]; then
  echo "Missing .venv Python/pip. Create the environment before upgrading." >&2
  exit 1
fi
if [[ ! -f "$REPO_ROOT/.env" ]]; then
  echo "Missing $REPO_ROOT/.env" >&2
  exit 1
fi
if [[ "$(git branch --show-current)" != "main" ]]; then
  echo "Upgrade must run from the main branch." >&2
  exit 1
fi

echo "==> Backing up and pausing trading-bot cron jobs"
if crontab -l >"$CRON_BACKUP" 2>/dev/null; then
  CRON_WAS_PRESENT=1
else
  : >"$CRON_BACKUP"
fi

awk -v marker="$PAUSE_MARKER" '
  index($0, "scripts/paper.sh") &&
      $0 !~ /^[[:space:]]*#/ &&
      $0 !~ /^# TRADING-BOT-UPGRADE-PAUSED / {
    print marker $0
    next
  }
  { print }
' "$CRON_BACKUP" >"$CRON_PAUSED_FILE"

paused_count="$(grep -c '^# TRADING-BOT-UPGRADE-PAUSED ' "$CRON_PAUSED_FILE" || true)"
if [[ "$paused_count" -eq 0 ]]; then
  echo "No active scripts/paper.sh crontab lines were found; refusing to continue." >&2
  exit 1
fi
echo "Paused $paused_count trading-bot cron line(s)."
crontab "$CRON_PAUSED_FILE"
CRON_IS_PAUSED=1

echo "==> Waiting for any in-flight bot jobs to finish"
exec 201>"$STATE_DIR/paper-daily.lock"
exec 202>"$STATE_DIR/paper-daily2x.lock"
exec 203>"$STATE_DIR/paper-weekly.lock"
exec 204>"$STATE_DIR/paper-health.lock"
exec 205>"$STATE_DIR/paper-health2x.lock"
flock 201
flock 202
flock 203
flock 204
flock 205

if ! git diff --quiet HEAD -- reports/paper; then
  echo "==> Stashing tracked server-generated paper reports"
  git stash push --quiet -m "upgrade-$STAMP paper reports" -- reports/paper
  REPORT_STASH_REF="stash@{0}"
  REPORT_STASHED=1
fi

echo "==> Fast-forwarding from origin/main"
git pull --ff-only origin main

echo "==> Installing declared dependencies"
"$PIP" install -r "$REPO_ROOT/requirements.txt"

echo "==> Running test suite"
"$PYTHON" -m pytest

echo "==> Running base paper pipeline without orders or local writes"
"$PYTHON" -m scripts.run_daily --dry-run --force

echo "==> Running 2x paper pipeline without orders or local writes"
"$PYTHON" -m scripts.run_daily --dry-run --force --profile 2x

echo "==> Checking base paper account health"
"$PYTHON" -m scripts.healthcheck --allow-pristine

echo "==> Checking 2x paper account health"
"$PYTHON" -m scripts.healthcheck --profile 2x --allow-pristine

restore_report_stash

echo "==> Restoring original crontab"
if [[ "$CRON_WAS_PRESENT" -eq 1 ]]; then
  crontab "$CRON_BACKUP"
else
  crontab -r 2>/dev/null || true
fi
CRON_IS_PAUSED=0
UPGRADE_SUCCEEDED=1

echo "==> Restarting the monitoring dashboard"
if systemctl --user list-unit-files trading-bot-dashboard.service &>/dev/null; then
  if systemctl --user restart trading-bot-dashboard.service; then
    echo "Dashboard restarted."
  else
    echo "WARNING: dashboard restart failed; check 'systemctl --user status trading-bot-dashboard.service'." >&2
  fi
else
  echo "Dashboard service unit not installed; skipping restart."
fi

echo
echo "UPGRADE VERIFIED."
echo "Original crontab restored. Trading-bot cron jobs are active."
echo "Backup retained at: $CRON_BACKUP"
