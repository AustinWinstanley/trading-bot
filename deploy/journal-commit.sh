#!/usr/bin/env bash
# Nightly journal commit: pull dev-committed research into the checkout the
# engine mounts read-write, then commit and push whatever the engine wrote
# to reports/paper*/ (its daily/weekly run notes) during the day.
#
# Fast-forward only is load-bearing, not a style choice: this must never
# merge. A merge here could silently rewrite history the way the engine or
# a human expects to be append-only, and a conflict is something a human
# should see and resolve, not something this script should paper over. On
# failure it exits non-zero, logs, and does nothing further — the next
# scheduled run tries again; nothing here is retried in-process.
#
# Everything is appended to logs/paper-YYYYMMDD.log in scripts/paper.sh's
# own block format, not just to the container's stdout: that file is what
# the dashboard's attention signal and scripts/weekly.py's CRITICAL scrape
# read. Before 2026-09-21 this job's failures reached `docker logs` only,
# and it failed every night for five weeks without anyone being told.
set -uo pipefail

REPO=${JOURNAL_REPO:-/repo}
LOG_DIR=${PAPER_LOG_DIR:-$REPO/logs}
mkdir -p "$LOG_DIR"
LOG=$LOG_DIR/paper-$(date -u +%Y%m%d).log

run() {
  cd "$REPO" || { echo "CRITICAL: cannot cd to $REPO"; return 1; }

  # Fetch and fast-forward are separate steps because they fail for
  # unrelated reasons and need different fixes. The old single
  # `git pull --ff-only` reported an SSH authentication failure as "checkout
  # has diverged" for five weeks.
  if ! git fetch origin main; then
    echo "CRITICAL: git fetch failed — cannot reach origin (deploy key, ssh, or network); this is NOT a divergence"
    return 1
  fi
  if ! git merge --ff-only FETCH_HEAD; then
    echo "CRITICAL: checkout has diverged from origin/main, needs human resolution — local-only commits:"
    git log --oneline FETCH_HEAD..HEAD
    return 1
  fi

  # reports/paper*/ covers both reports/paper/ (base) and reports/paper_2x/
  # (2x) — the only paths this service ever writes to git.
  git add reports/paper*/
  if git diff --cached --quiet; then
    echo "nothing to commit"
  else
    git -c user.name="trading-bot journal" -c user.email="journal@noreply.local" \
      commit -q -m "chore(journal): paper reports" || { echo "CRITICAL: git commit failed"; return 1; }
  fi

  # Push whenever the checkout is ahead, not only after a commit made just
  # now: a push that failed last night leaves commits that a quiet day
  # ("nothing to commit") would otherwise never retry.
  ahead=$(git rev-list --count FETCH_HEAD..HEAD)
  if [ "$ahead" -gt 0 ]; then
    if ! git push origin main; then
      echo "CRITICAL: git push failed with $ahead unpushed commit(s) — will retry next scheduled run"
      return 1
    fi
    echo "pushed $ahead commit(s)"
  fi
}

{
  echo "=== $(date -u '+%F %T UTC') job=journal slot=any ==="
  run
  rc=$?
  [ $rc -ne 0 ] && echo "CRITICAL: job=journal exited rc=$rc"
  echo "=== end rc=$rc ==="
  exit $rc
} 2>&1 | tee -a "$LOG"
exit "${PIPESTATUS[0]}"
