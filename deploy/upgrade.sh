#!/usr/bin/env bash
# Fail-closed upgrade for the CONTAINERIZED deployment — refreshes the git
# checkout and switches all four compose services to a single release tag.
#
# Successor to scripts/upgrade.sh for a server that has cut over from host
# cron to `docker compose -f deploy/docker-compose.yml`. Every release
# publishes all four images (engine/journal/dashboard/mcp-server) under the
# same version, so this script upgrades all four to that one tag rather
# than tracking per-service versions independently.
#
# engine carries broker credentials and is the only service that places
# orders, so it gets the full treatment: pytest + both dry-runs + both
# healthchecks (all mutation-free) run against the CANDIDATE image via a
# one-off `compose run` container BEFORE the live engine service is ever
# touched, then a bounded post-switch health poll with automatic rollback.
#
# dashboard/mcp-server/journal are secretless (dashboard/mcp-server are
# also read-only; journal holds a repo-scoped git key and nothing else) —
# a bad image there isn't a trading risk, so they get a lighter
# pull-switch-poll-rollback pass that reuses each service's own compose
# healthcheck as the verification instead of a separate test battery.
# dashboard's healthcheck is a real HTTP call to a live endpoint, not just
# a liveness ping; mcp-server's is a bare TCP connect; journal's mirrors
# engine's own pgrep-supercronic check.
#
# scripts/upgrade.sh still governs the pre-cutover host-cron deployment and
# is intentionally left alone; see docs/operations.md for which one applies
# to your deployment.
#
# Usage: deploy/upgrade.sh [<version-tag>]
#   deploy/upgrade.sh          # git pull, then upgrade to whatever version.txt says
#   deploy/upgrade.sh v0.2.0   # git pull, then upgrade to this specific tag
set -Eeuo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEPLOY_DIR="$REPO_ROOT/deploy"
COMPOSE_FILE="$DEPLOY_DIR/docker-compose.yml"
ENV_FILE="$DEPLOY_DIR/.env"
STATE_DIR="$REPO_ROOT/state"

if [[ $# -gt 1 ]]; then
  echo "usage: $0 [<version-tag>]" >&2
  exit 2
fi

# Re-exec once after pulling, so the rest of this run reads whatever
# version of THIS script the pull just brought in — an in-place git pull
# without this would leave bash mid-execution against a script file that
# changed size out from under it. _UPGRADE_REEXECD guards against pulling
# (and re-execing) a second time once we're already running the fresh copy.
if [[ -z "${_UPGRADE_REEXECD:-}" ]]; then
  echo "==> Pulling latest from origin/main"
  git -C "$REPO_ROOT" pull --ff-only origin main
  export _UPGRADE_REEXECD=1
  # An absolute path, not "$0" — if this was invoked as a bare relative
  # filename (e.g. `bash upgrade.sh` from inside deploy/), `exec "$0"`
  # searches $PATH for a slash-less name and fails with "not found" instead
  # of finding it in the current directory. Confirmed by hitting exactly
  # this failure while testing this script before it shipped.
  exec "$DEPLOY_DIR/upgrade.sh" "$@"
fi

if [[ -n "${1:-}" ]]; then
  CANDIDATE_TAG="$1"
else
  CANDIDATE_TAG="v$(cat "$REPO_ROOT/version.txt")"
fi

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Missing $ENV_FILE — copy deploy/.env.example and fill it in first." >&2
  exit 1
fi

_tag_var() {  # engine -> ENGINE_TAG, mcp-server -> MCP_TAG, etc.
  case "$1" in
    engine) echo ENGINE_TAG ;;
    dashboard) echo DASHBOARD_TAG ;;
    mcp-server) echo MCP_TAG ;;
    journal) echo JOURNAL_TAG ;;
  esac
}

_current_tag() {
  grep "^$(_tag_var "$1")=" "$ENV_FILE" | tail -1 | cut -d= -f2-
}

PREVIOUS_ENGINE_TAG="$(_current_tag engine)"
if [[ -z "$PREVIOUS_ENGINE_TAG" ]]; then
  echo "No existing ENGINE_TAG in $ENV_FILE — set an initial value before upgrading." >&2
  exit 1
fi

COMPOSE=(docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE")

UPGRADE_SUCCEEDED=0

on_exit() {
  rc=$?
  if [[ "$UPGRADE_SUCCEEDED" -eq 1 ]]; then
    exit "$rc"
  fi
  echo
  echo "UPGRADE FAILED (exit $rc)."
  echo "Any service not explicitly reported above as switched is still on its previous tag."
  echo "No orders were submitted by this script."
  echo "To retry once the candidate is fixed:  $0 $CANDIDATE_TAG"
  exit "$rc"
}
trap on_exit EXIT

echo "==> Target version: $CANDIDATE_TAG (engine was $PREVIOUS_ENGINE_TAG)"
echo "==> Pulling all four candidate images"
ENGINE_TAG="$CANDIDATE_TAG" DASHBOARD_TAG="$CANDIDATE_TAG" MCP_TAG="$CANDIDATE_TAG" JOURNAL_TAG="$CANDIDATE_TAG" \
  "${COMPOSE[@]}" pull

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

# --dashboard/--mcp-server tests never load — they're outside the trusted
# tier's image entirely (see deploy/engine.Dockerfile's comment on this).
PYTEST_IGNORES=(--ignore=tests/dashboard --ignore=tests/mcp_server)

echo "==> Running test suite inside the candidate engine image"
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
# Five separate calls, not `flock -u 201 202 203 204 205`: with no command
# given, flock unlocks exactly one fd per invocation — anything after the
# first numeric argument is treated as a command to exec, which is exactly
# how this failed live on 2026-08-18 ("flock: failed to execute 202: No
# such file or directory", after every verification step upstream had
# already passed). Never actually exercised end-to-end before that run —
# earlier testing of this script isolated the health-poll/rollback logic
# rather than running the full script against real locks.
flock -u 201
flock -u 202
flock -u 203
flock -u 204
flock -u 205

echo "==> Switching the live engine service to $CANDIDATE_TAG"
"${COMPOSE[@]}" stop engine
sed -i.bak "s/^ENGINE_TAG=.*/ENGINE_TAG=$CANDIDATE_TAG/" "$ENV_FILE"
rm -f "$ENV_FILE.bak"
"${COMPOSE[@]}" up -d engine

# `up -d` returns as soon as the container starts, not once it's actually
# healthy — the pytest/dry-run/healthcheck battery above all ran via
# `compose run`, a separate one-off container, so it cannot catch a
# candidate that only fails once running as the real supercronic PID 1
# under the real compose `user:`/volumes contract (e.g. a bad CMD, a
# missing runtime file, a crash-loop). Poll the engine service's own
# healthcheck (pgrep supercronic; see docker-compose.yml) before declaring
# victory, and auto-rollback to $PREVIOUS_ENGINE_TAG if it never goes
# healthy — otherwise this script would print "VERIFIED AND SWITCHED" with
# the live engine actually down and the previous, working image already
# stopped.
echo "==> Waiting for the switched engine service to report healthy"
HEALTHY=0
for _ in $(seq 1 24); do  # up to ~4min: start_period 10s + 3 retries * 60s interval, plus slack
  STATUS="$(docker inspect --format '{{.State.Health.Status}}' trading-bot-engine 2>/dev/null || echo "unknown")"
  if [[ "$STATUS" == "healthy" ]]; then
    HEALTHY=1
    break
  fi
  if [[ "$STATUS" == "unhealthy" ]]; then
    break
  fi
  sleep 10
done

if [[ "$HEALTHY" -ne 1 ]]; then
  echo "Candidate $CANDIDATE_TAG never reported healthy (status: $STATUS) — rolling back engine." >&2
  "${COMPOSE[@]}" stop engine
  sed -i.bak "s/^ENGINE_TAG=.*/ENGINE_TAG=$PREVIOUS_ENGINE_TAG/" "$ENV_FILE"
  rm -f "$ENV_FILE.bak"
  "${COMPOSE[@]}" up -d engine
  echo "Rolled back engine to $PREVIOUS_ENGINE_TAG. Inspect the candidate before retrying:" >&2
  echo "  docker logs trading-bot-engine" >&2
  exit 1
fi
echo "engine: switched to $CANDIDATE_TAG, confirmed healthy (was $PREVIOUS_ENGINE_TAG)."

switch_light_service() {
  local svc="$1" var previous healthy status container
  var="$(_tag_var "$svc")"
  previous="$(_current_tag "$svc")"
  echo "==> Switching $svc to $CANDIDATE_TAG (was ${previous:-unset})"
  sed -i.bak "s/^${var}=.*/${var}=$CANDIDATE_TAG/" "$ENV_FILE"
  rm -f "$ENV_FILE.bak"
  "${COMPOSE[@]}" up -d "$svc"

  healthy=0
  container="trading-bot-$svc"
  for _ in $(seq 1 13); do  # ~130s: covers these services' own start_period + 3 retries, plus slack
    status="$(docker inspect --format '{{.State.Health.Status}}' "$container" 2>/dev/null || echo "unknown")"
    if [[ "$status" == "healthy" ]]; then healthy=1; break; fi
    if [[ "$status" == "unhealthy" ]]; then break; fi
    sleep 10
  done

  if [[ "$healthy" -ne 1 ]]; then
    echo "$svc: $CANDIDATE_TAG never reported healthy (status: $status) — rolling back." >&2
    if [[ -n "$previous" ]]; then
      sed -i.bak "s/^${var}=.*/${var}=$previous/" "$ENV_FILE"
      rm -f "$ENV_FILE.bak"
      "${COMPOSE[@]}" up -d "$svc"
      echo "Rolled back $svc to $previous. Inspect before retrying:  docker logs $container" >&2
    else
      echo "No previous tag on record for $svc — not auto-rolling back; inspect manually:  docker logs $container" >&2
    fi
    return 1
  fi
  echo "$svc: switched to $CANDIDATE_TAG, confirmed healthy (was ${previous:-unset})."
}

LIGHT_FAILED=0
for svc in dashboard mcp-server journal; do
  switch_light_service "$svc" || LIGHT_FAILED=1
done

if [[ "$LIGHT_FAILED" -eq 1 ]]; then
  exit 1
fi

UPGRADE_SUCCEEDED=1
echo
echo "UPGRADE VERIFIED AND SWITCHED — all four services now on $CANDIDATE_TAG."
echo "Rollback if needed, per service:"
echo "  ENGINE_TAG=$PREVIOUS_ENGINE_TAG ${COMPOSE[*]} up -d engine"
echo "  (and similarly for dashboard/mcp-server/journal with their own *_TAG)"
