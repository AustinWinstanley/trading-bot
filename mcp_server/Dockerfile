# Build from the repo root: docker build -f mcp_server/Dockerfile ..
# (deploy/docker-compose.yml sets this context/dockerfile pair automatically.)
FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dependency layer first so code-only changes don't invalidate the pip cache.
COPY mcp_server/requirements.txt ./mcp_server/requirements.txt
RUN pip install --no-cache-dir -r mcp_server/requirements.txt

# Only dashboard/__init__.py + db.py — the pure, reusable read-only data
# layer this package builds on — never dashboard/routes.py or app.py
# (which import Flask; this image has no Flask dependency at all, see
# requirements.txt and tests/mcp_server/test_safety.py). Exactly the two
# pure engine modules the dashboard image also copies, nothing else from
# engine/ (not engine/execute.py, not engine/data.py — those touch the
# Alpaca client). __init__.py is 0 bytes but copied verbatim so the
# package structure matches the source tree exactly.
COPY dashboard/__init__.py dashboard/db.py dashboard/files.py ./dashboard/
COPY engine/__init__.py engine/attribution.py engine/config.py ./engine/
COPY mcp_server/ ./mcp_server/

RUN groupadd --system --gid 10002 mcpserver \
    && useradd --system --uid 10002 --gid mcpserver --no-create-home mcpserver \
    && chown -R mcpserver:mcpserver /app
USER mcpserver

EXPOSE 8788

# Binds all of the container's own interfaces — this is unrelated to host-
# level exposure, which is controlled entirely by deploy/docker-compose.yml's
# port mapping (currently "0.0.0.0:8788:8788", LAN-reachable — see that
# file's mcp-server service comment, which mirrors the dashboard service's
# rationale for the same choice). 127.0.0.1 here would bind only the
# container's OWN loopback, which Docker's port-publishing NAT can never
# reach regardless of the host-side mapping, so this must stay 0.0.0.0 even
# when the host side is loopback-only.
#
# Unlike the dashboard's gunicorn (WSGI), MCPServer's streamable-http
# transport hosts its own ASGI server (uvicorn) internally via .run() — no
# separate WSGI/ASGI server wrapper needed here.
CMD ["python", "-m", "mcp_server.server"]
