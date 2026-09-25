"""Handler for POST /ask: accepts a question and queues it for the agent.

The Lambda is a gatekeeper, not the brains (pre-research/agentcore/AUTH.md):
it validates the request, settles the facts the model should not guess at (the
address, the heading), and hands the result to the queue. The agent is called
by handlers/worker.py, and the app collects the answer from
handlers/result.py.

⚠️ This endpoint used to wait for the answer and return it. It no longer does:
API Gateway caps a request at 29s and the agent alone measured 25.5s, so the
wait was moved off the request path entirely (docs/01_architecture.md section
5). The response is now 202 with a requestId to poll.
"""

import json
import os
import time
import uuid
from typing import Any

import boto3

from lib import prompt as prompt_builder
from lib import store

# Answers are read aloud while riding, so a question that long is a mistake
# (and caps input cost - docs/01_architecture.md section 9).
MAX_QUESTION_CHARS = 500

# AgentCore rejects a runtimeSessionId below this length.
MIN_SESSION_ID_CHARS = 33

QUEUE_URL = os.environ.get("QUEUE_URL", "")

# Created once per container so warm invocations skip client setup.
_sqs = boto3.client("sqs")


def _response(status: int, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, ensure_ascii=False),
    }


def _new_session_id() -> str:
    # uuid4().hex is 32 chars, one short of the minimum, so it is prefixed.
    return f"touring-{uuid.uuid4().hex}"


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    if not QUEUE_URL:
        return _response(502, {"error": "QUEUE_URL is not configured."})

    # API Gateway (proxy integration) passes the body as a JSON string.
    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _response(400, {"error": "Request body must be valid JSON."})
    if not isinstance(body, dict):
        return _response(400, {"error": "Request body must be a JSON object."})

    question = body.get("question")
    if not isinstance(question, str) or not question.strip():
        return _response(400, {"error": "`question` is required and must be non-empty."})
    question = question.strip()
    if len(question) > MAX_QUESTION_CHARS:
        return _response(
            400,
            {"error": f"`question` must be at most {MAX_QUESTION_CHARS} characters."},
        )

    # The app owns the session id and holds on to it; a request without one
    # starts a new conversation.
    session_id = body.get("sessionId")
    if session_id is None:
        session_id = _new_session_id()
    elif not isinstance(session_id, str) or len(session_id) < MIN_SESSION_ID_CHARS:
        return _response(
            400,
            {"error": f"`sessionId` must be at least {MIN_SESSION_ID_CHARS} characters."},
        )

    # The address is resolved here rather than in the worker so that a failure
    # to place the rider surfaces while the app is still on the request, and so
    # the coordinates never have to be written to the table.
    started = time.monotonic()
    prompt = prompt_builder.build(
        question, body.get("start"), body.get("end"), body.get("elapsedSeconds")
    )
    geocode_ms = (time.monotonic() - started) * 1000

    request_id = str(uuid.uuid4())
    try:
        store.create_pending(request_id, session_id, prompt)
        _sqs.send_message(
            QueueUrl=QUEUE_URL,
            MessageBody=json.dumps({"requestId": request_id}),
        )
    except Exception as error:  # noqa: BLE001 - surface one shape to the client
        # Logged for CloudWatch; the client gets a generic message rather than
        # the raw AWS error, which can name internal resources.
        print(f"failed to queue question: {type(error).__name__}: {error}")
        return _response(502, {"error": "The question could not be accepted."})

    # Neither coordinates nor the resolved address are logged: where the rider
    # has been is theirs (docs/03_dynamodb_table.md section 4).
    print(f"timing: geocode={geocode_ms:.0f}ms queued={request_id}")

    # 202: accepted, not answered. The app polls GET /ask/{requestId}.
    return _response(202, {"requestId": request_id, "sessionId": session_id})
