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

# 0.0.0.0, deliberately different from the previous systemd deployment's
# 127.0.0.1: inside the container, 127.0.0.1 is the container's OWN
# loopback, which Docker's port-publishing NAT cannot reach — a process
# bound only to container-internal loopback is unreachable even via
# `docker run -p`. The container must listen on all its own interfaces;
# host-level exposure is restricted separately by
# deploy/docker-compose.yml's "127.0.0.1:8787:8787" port mapping, which is
# the actual security boundary here, not anything inside the container.
CMD ["gunicorn", "--worker-class", "gthread", "--workers", "2", "--threads", "4", \
     "--bind", "0.0.0.0:8787", \
     "--access-logfile", "-", "--error-logfile", "-", \
     "dashboard.wsgi:app"]
