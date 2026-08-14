# Build from the repo root: docker build -f deploy/journal.Dockerfile ..
# (deploy/docker-compose.yml sets this context/dockerfile pair automatically.)
#
# Nightly committer for the git checkout the engine service mounts
# read-write. Not part of the trusted (broker-credential) tier and not part
# of the read-only (secretless) tier either — a third, narrow tier of its
# own: it holds a repo-scoped git deploy key and nothing else. See
# docs/architecture.md.
#
# Deliberately NOT python:3.13-slim: this image runs one job (git add/
# commit/push on a schedule) and needs none of the Python dependency stack
# — alpine + git + openssh-client + supercronic keeps it small and keeps
# its dependency surface unrelated to the engine's.
FROM alpine:3.24

# tzdata is required for CRON_TZ (Go's time.LoadLocation needs the IANA
# zoneinfo database on disk) — unlike deploy/engine.Dockerfile's
# python:3.13-slim base, Alpine does not ship it by default; omitting this
# fails supercronic at startup with "unknown time zone America/New_York"
# (verified locally before adding this line).
RUN apk add --no-cache git openssh-client bash tzdata

ARG SUPERCRONIC_VERSION=v0.2.36
# Verified 2026-08-14 the same way as deploy/engine.Dockerfile: GitHub's
# release-asset API digest field plus an independent local `sha256sum`.
ARG SUPERCRONIC_SHA256=005e14ccaffd1eacf4dca2493f54cad13670fc39017920db9ba9db19d1ed5383
ARG SUPERCRONIC=supercronic-linux-amd64
RUN wget -qO /usr/local/bin/supercronic \
      "https://github.com/aptible/supercronic/releases/download/${SUPERCRONIC_VERSION}/${SUPERCRONIC}" \
    && echo "${SUPERCRONIC_SHA256}  /usr/local/bin/supercronic" | sha256sum -c - \
    && chmod +x /usr/local/bin/supercronic

WORKDIR /app
COPY deploy/journal-crontab /app/crontab
COPY deploy/journal-commit.sh /usr/local/bin/journal-commit
RUN chmod +x /usr/local/bin/journal-commit

# Pinned github.com host keys fetched from GitHub's own published `/meta`
# API (https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/githubs-ssh-key-fingerprints),
# not accepted on first connect — an automated, non-interactive `git push`
# must never disable host-key checking (that would accept any MITM'd key
# silently). Regenerate deploy/known_hosts.github the same way if GitHub
# ever rotates these.
COPY deploy/known_hosts.github /etc/ssh/ssh_known_hosts

RUN addgroup -g 10004 journal && adduser -D -u 10004 -G journal journal
# NOT `USER journal` — compose overrides the runtime user to the host
# uid:gid that owns the mounted checkout (`:/repo`), same reasoning as
# deploy/engine.Dockerfile's own comment on this.

CMD ["supercronic", "-passthrough-logs", "/app/crontab"]
