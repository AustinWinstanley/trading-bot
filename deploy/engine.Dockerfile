# Build from the repo root: docker build -f deploy/engine.Dockerfile ..
# (deploy/docker-compose.yml sets this context/dockerfile pair automatically.)
#
# Unlike dashboard/Dockerfile and mcp_server/Dockerfile, this image is the
# trusted tier: it holds broker credentials (via compose's env_file, never
# baked into the image) and is the only container that writes to state/,
# reports/, or logs/. It gets the FULL engine/backtest/scripts tree — not
# the selective per-module COPY the two read-only services use — because
# scripts/iwm_breakout_forward.py (a cron job here) imports backtest.intraday
# and backtest.intraday_strategy_study, and because deploy/upgrade.sh runs
# the test suite inside a candidate image before switching to it.
#
# config.yaml/config_2x.yaml/deploy/crontab are baked into the image, not
# volume-mounted: "the image is what runs" is now literally true for the
# trading logic, matching the promotion discipline in AGENTS.md — an edit
# only takes effect through a new image tag, never a live directory edit.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PAPER_BOT_ROOT=/app \
    PAPER_PYTHON=/usr/local/bin/python3

# supercronic is a single static binary built for exactly this job: a
# container-native cron replacement that understands CRON_TZ, so the
# EDT/EST crontab-line-pair trick in scripts/paper.sh's slot guard becomes
# unnecessary going forward (kept as a no-op safety net — see
# docs/architecture.md). Pinned by version + sha256, not "latest".
# v0.2.34 and earlier fatal on startup as PID 1 ("Failed to fork exec: no
# such file or directory") — a working-directory/os.Executable() resolution
# bug in the reaper (aptible/supercronic#177), fixed in v0.2.36. Confirmed
# reproduced against v0.2.34 and resolved against v0.2.36 by actually
# building and running this image locally before pinning this version.
ARG SUPERCRONIC_VERSION=v0.2.36
# Verified 2026-08-14 against both GitHub's release-asset API digest field
# and an independent local `shasum -a 256` of the downloaded binary.
ARG SUPERCRONIC_SHA256=005e14ccaffd1eacf4dca2493f54cad13670fc39017920db9ba9db19d1ed5383
ARG SUPERCRONIC=supercronic-linux-amd64
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl util-linux procps \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSLo /usr/local/bin/supercronic \
         "https://github.com/aptible/supercronic/releases/download/${SUPERCRONIC_VERSION}/${SUPERCRONIC}" \
    && echo "${SUPERCRONIC_SHA256}  /usr/local/bin/supercronic" | sha256sum -c - \
    && chmod +x /usr/local/bin/supercronic

WORKDIR /app

# Dependency layer first so code-only changes don't invalidate the pip cache.
COPY requirements.txt constraints.txt ./
RUN pip install --no-cache-dir -r requirements.txt -c constraints.txt

# The full trusted tier. tests/ ships too, so `deploy/upgrade.sh` can run
# the suite against the candidate image itself before switching to it — but
# this image deliberately does NOT COPY dashboard/ or mcp_server/ (kept out
# of the trusted tier), so `tests/dashboard/` and `tests/mcp_server/` fail
# collection here; run pytest with `--ignore=tests/dashboard
# --ignore=tests/mcp_server` in this image specifically. An in-image test
# run also needs state/ and reports/ bind-mounted and writable by whatever
# uid runs it (matching the real `user:`/volumes contract in
# docker-compose.yml) — several tests write real lock files and load real
# reports/experiments/*.json registrations; verified locally that running
# standalone with neither mounted produces confusing, unrelated-looking
# failures (a config-validation error for a missing registration file, and
# a permission-denied opening a lock file) rather than a clean skip.
COPY engine/ ./engine/
COPY backtest/ ./backtest/
COPY scripts/ ./scripts/
COPY tests/ ./tests/
COPY config.yaml config_2x.yaml pytest.ini ./
COPY deploy/crontab ./deploy/crontab

RUN groupadd --system --gid 10003 engine \
    && useradd --system --uid 10003 --gid engine --no-create-home engine \
    # state/, reports/, logs/ are bind-mounted at runtime (compose's `user:`
    # override sets the host uid/gid that owns them — see docker-compose.yml)
    # so the mountpoints' in-image ownership is irrelevant there; created and
    # chowned here anyway so the image is also runnable standalone (no bind
    # mounts) as engine, e.g. for a local smoke test. mkdir MUST precede
    # chown -R, not follow it, or these three directories stay root-owned.
    && mkdir -p /app/state /app/reports /app/logs \
    && chown -R engine:engine /app

# NOT `USER engine` — compose overrides the runtime user to the host uid:gid
# that owns state/reports/logs (see docker-compose.yml's `user:` key), since
# a bind mount's on-disk ownership is whatever the host process already set
# and doesn't shift to match an in-image user. The image build itself still
# runs as root only for the chown above; supercronic and every job it
# spawns run as whatever compose sets at container start.

# supercronic passes each job's stdout/stderr through to the container's
# own, which `docker logs` captures — scripts/paper.sh's own dated
# logs/paper-*.log remain the primary record (scraped by scripts/weekly.py),
# this is a secondary, ephemeral view for `docker compose logs engine`.
CMD ["supercronic", "-passthrough-logs", "/app/deploy/crontab"]
