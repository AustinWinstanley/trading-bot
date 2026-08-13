"""Run entrypoint — the container CMD. A single developer's Claude Code
session polling a monitoring server, not a multi-client stateful
workflow, so stateless_http=True avoids session-affinity/reconnect
complexity for no real cost here.
"""

from __future__ import annotations

import os

from .app import create_server


def main() -> None:
    host = os.environ.get("MCP_SERVER_HOST", "0.0.0.0")
    port = int(os.environ.get("MCP_SERVER_PORT", "8788"))
    create_server().run(
        transport="streamable-http",
        host=host,
        port=port,
        stateless_http=True,
    )


if __name__ == "__main__":
    main()
