#!/usr/bin/env bash
# Fail-closed engine upgrade for the CONTAINERIZED deployment.
#
# Successor to scripts/upgrade.sh for a server that has cut over from host
# cron to `docker compose -f deploy/docker-compose.yml`. Verifies a
# candidate engine image (pytest, both dry-runs, both healthchecks — all
# mutation-free) BEFORE switching the live `engine` service to it, so a bad
# image is never live even briefly. The already-running engine service, on
# its current (old) tag, is untouched until every check passes.
#
# scripts/upgrade.sh still governs the pre-cutover host-cron deployment and
# is intentionally left alone; see docs/operations.md for which one applies
# to your deployment.
#
# Usage: deploy/upgrade.sh <engine-image-tag>
#   e.g. deploy/upgrade.sh v0.2.0
set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEPLOY_DIR="$REPO_ROOT/deploy"
COMPOSE_FILE="$DEPLOY_DIR/docker-compose.yml"
ENV_FILE="$DEPLOY_DIR/.env"
STATE_DIR="$REPO_ROOT/state"

if [[ $# -ne 1 ]]; then
  echo "usage: $0 <engine-image-tag>" >&2
  exit 2
fi
CANDIDATE_TAG="$1"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Missing $ENV_FILE — copy deploy/.env.example and fill it in first." >&2
  exit 1
fi
PREVIOUS_TAG="$(grep '^ENGINE_TAG=' "$ENV_FILE" | tail -1 | cut -d= -f2-)"
if [[ -z "$PREVIOUS_TAG" ]]; then
  echo "No existing ENGINE_TAG in $ENV_FILE — set an initial value before upgrading." >&2
  exit 1
fi

COMPOSE=(docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE")

LOCKS_HELD=0
UPGRADE_SUCCEEDED=0

on_exit() {
  rc=$?
  if [[ "$UPGRADE_SUCCEEDED" -eq 1 ]]; then
    exit "$rc"
  fi
  echo
  echo "UPGRADE FAILED (exit $rc)."
  echo "The live engine service was NOT switched — it is still running $PREVIOUS_TAG."
  echo "No orders were submitted by this script."
  echo "To retry once the candidate image is fixed:  $0 $CANDIDATE_TAG"
  echo "To roll back manually if the engine was already switched:"
  echo "  ENGINE_TAG=$PREVIOUS_TAG ${COMPOSE[*]} up -d engine"
  exit "$rc"
}
trap on_exit EXIT

echo "==> Pulling candidate image: engine:$CANDIDATE_TAG (previous: $PREVIOUS_TAG)"
ENGINE_TAG="$CANDIDATE_TAG" "${COMPOSE[@]}" pull engine

echo "==> Waiting for any in-flight engine jobs to finish (draining locks)"
# Same five locks scripts/upgrade.sh has always drained — daily/daily2x/
# weekly cover the long-running full rebalances and the weekly rebuild;
# health/health2x are quick but included for the same reason they always
# were. shadows2x/iwmfwd/options_daily2x/momls2x are not drained here,
# matching that existing precedent (see docs/architecture.md's lock table
# for why sharing rather than every job getting a slot here is correct).
mkdir -p "$STATE_DIR"
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
LOCKS_HELD=1

# --dashboard/--mcp-server tests never load — they're outside the trusted
# tier's image entirely (see deploy/engine.Dockerfile's comment on this).
PYTEST_IGNORES=(--ignore=tests/dashboard --ignore=tests/mcp_server)

echo "==> Running test suite inside the candidate image"
ENGINE_TAG="$CANDIDATE_TAG" "${COMPOSE[@]}" run --rm --no-deps engine \
  python -m pytest -q "${PYTEST_IGNORES[@]}"

echo "==> Running base paper pipeline without orders or local writes"
ENGINE_TAG="$CANDIDATE_TAG" "${COMPOSE[@]}" run --rm --no-deps engine \
  python -m scripts.run_daily --dry-run --force

echo "==> Running 2x paper pipeline without orders or local writes"
ENGINE_TAG="$CANDIDATE_TAG" "${COMPOSE[@]}" run --rm --no-deps engine \
  python -m scripts.run_daily --dry-run --force --profile 2x

echo "==> Checking base paper account health"
ENGINE_TAG="$CANDIDATE_TAG" "${COMPOSE[@]}" run --rm --no-deps engine \
  python -m scripts.healthcheck --allow-pristine

echo "==> Checking 2x paper account health"
ENGINE_TAG="$CANDIDATE_TAG" "${COMPOSE[@]}" run --rm --no-deps engine \
  python -m scripts.healthcheck --profile 2x --allow-pristine

# Release the locks now — verification is done, and the actual switch below
# (stop + up -d) needs no exclusion beyond what compose itself provides.
flock -u 201 202 203 204 205
LOCKS_HELD=0

echo "==> Switching the live engine service to $CANDIDATE_TAG"
"${COMPOSE[@]}" stop engine
sed -i.bak "s/^ENGINE_TAG=.*/ENGINE_TAG=$CANDIDATE_TAG/" "$ENV_FILE"
rm -f "$ENV_FILE.bak"
"${COMPOSE[@]}" up -d engine

UPGRADE_SUCCEEDED=1
echo
echo "UPGRADE VERIFIED AND SWITCHED."
echo "engine is now running $CANDIDATE_TAG (was $PREVIOUS_TAG)."
echo "Rollback if needed:  ENGINE_TAG=$PREVIOUS_TAG ${COMPOSE[*]} up -d engine"
