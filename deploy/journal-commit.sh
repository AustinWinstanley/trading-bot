#!/usr/bin/env bash
# Nightly journal commit: pull dev-committed research into the checkout the
# engine mounts read-write, then commit and push whatever the engine wrote
# to reports/paper*/ (its daily/weekly run notes) during the day.
#
# --ff-only is load-bearing, not a style choice: this must never merge. A
# merge here could silently rewrite history the way the engine or a human
# expects to be append-only, and a conflict is something a human should
# see and resolve, not something this script should paper over. On
# failure it exits non-zero, logs, and does nothing further — the next
# scheduled run tries again; nothing here is retried in-process.
set -euo pipefail

cd /repo

echo "=== $(date -u '+%F %T UTC') journal commit ==="

if ! git pull --ff-only origin main; then
  echo "CRITICAL: git pull --ff-only failed — checkout has diverged from origin/main, needs human resolution"
  exit 1
fi

# reports/paper*/ covers both reports/paper/ (base) and reports/paper_2x/
# (2x) — the only paths this service ever writes to git.
git add reports/paper*/

if git diff --cached --quiet; then
  echo "nothing to commit"
  exit 0
fi

git -c user.name="trading-bot journal" -c user.email="journal@noreply.local" \
  commit -m "chore(journal): paper reports"

if ! git push origin main; then
  echo "CRITICAL: git push failed after a successful commit — will retry next scheduled run"
  exit 1
fi

echo "=== journal commit done ==="
