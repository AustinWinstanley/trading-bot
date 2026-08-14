# Build from the repo root: docker build -f dashboard/Dockerfile ..
# (deploy/docker-compose.yml sets this context/dockerfile pair automatically.)
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dependency layer first so code-only changes don't invalidate the pip cache.
COPY dashboard/requirements.txt ./dashboard/requirements.txt
RUN pip install --no-cache-dir -r dashboard/requirements.txt

# Exactly the dashboard package...
COPY dashboard/ ./dashboard/
# ...and exactly the two pure engine modules it imports, nothing else from
# engine/ (not engine/execute.py, not engine/data.py — those touch the
# Alpaca client). __init__.py is 0 bytes but copied verbatim so the package
# structure matches the source tree exactly.
COPY engine/__init__.py engine/attribution.py engine/config.py ./engine/

RUN groupadd --system --gid 10001 dashboard \
    && useradd --system --uid 10001 --gid dashboard --no-create-home dashboard \
    && chown -R dashboard:dashboard /app
USER dashboard

EXPOSE 8787

# Binds all of the container's own interfaces — this is unrelated to host-
# level exposure, which is controlled entirely by deploy/docker-compose.yml's
# port mapping (currently "0.0.0.0:8787:8787", LAN-reachable — see that
# file's comment for why, and note the app has no auth of its own).
# 127.0.0.1 here would bind only the container's OWN loopback, which
# Docker's port-publishing NAT can never reach regardless of the host-side
# mapping, so this must stay 0.0.0.0 even when the host side is loopback-only.
# --no-control-socket: gunicorn 25.1.0+ added a control socket for the
# gunicornc runtime-management CLI (worker status/reload/shutdown),
# defaulting to $HOME/.gunicorn/gunicorn.ctl when $XDG_RUNTIME_DIR is
# unset. This container's non-root user is deliberately created with
# --no-create-home (see the groupadd/useradd step above), so gunicorn
# fails to create that directory and logs "Control server error:
# Permission denied" on every start — harmless (the HTTP server itself
# is unaffected; verified with a real request) but noisy, and nothing
# here ever runs gunicornc against this container anyway. Disabling it
# is simpler and more correct than granting a home directory just to
# host a socket nothing uses.
CMD ["gunicorn", "--worker-class", "gthread", "--workers", "2", "--threads", "4", \
     "--bind", "0.0.0.0:8787", \
     "--access-logfile", "-", "--error-logfile", "-", \
     "--no-control-socket", \
     "dashboard.wsgi:app"]
