"""Health check handler for GET /health.

Lightweight liveness check: no auth, no parameters, fixed response. Useful for
verifying app <-> backend wiring and for monitoring, independent of /ask.
"""

import json
from typing import Any


def handler(_event: dict[str, Any], _context: Any) -> dict[str, Any]:
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"status": "ok"}),
    }
