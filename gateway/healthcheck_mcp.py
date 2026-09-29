"""
Docker HEALTHCHECK for the gateway-mcp service (Milestone 2's streamable-
http MCP endpoint). A bare GET to /mcp with no token correctly gets 401
from HermesBearerAuthMiddleware (see gateway/mcp_auth.py) - that IS the
"healthy" signal here (the process is up and serving HTTP); this only
fails the healthcheck on an actual connection error (process down/still
starting), never on the expected 401.
"""

from __future__ import annotations

import os
import sys
import urllib.error
import urllib.request


def main() -> int:
    port = os.environ.get("MCP_HTTP_PORT", "8091")
    try:
        urllib.request.urlopen(f"http://localhost:{port}/mcp", timeout=3)
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            return 0
        print(f"unexpected HTTP status {exc.code}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - any connection failure means unhealthy
        print(f"connection failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
