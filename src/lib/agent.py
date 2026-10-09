"""Calling the AgentCore runtime.

Split out of handlers/ask.py when the flow went async: the call now happens in
the worker, well away from the API Gateway request that used to time out
around it.

Two things to know about the shape of this code:

  - The runtime lives in us-east-1 while these Lambdas run in ap-northeast-1,
    because the web-search connector is only offered there. The region is
    therefore explicit rather than inherited from the environment.
  - The runtime replies with an SSE stream of Strands events, so the answer
    arrives as a series of text deltas that have to be reassembled.
"""

import json
import os
from typing import Any, Optional

import boto3

# Where the agent runs. Deliberately not this Lambda's own region; see above.
AGENT_REGION = os.environ.get("AGENT_REGION", "us-east-1")

# Set from the SAM template. Without it there is nothing to call.
AGENT_ARN = os.environ.get("AGENT_ARN", "")

# Created once per container so warm invocations skip client setup.
_client = boto3.client("bedrock-agentcore", region_name=AGENT_REGION)


def _is_tool_use_start(event: dict) -> bool:
    """True when the model starts calling a tool (e.g. web search)."""
    start = event.get("event", {}).get("contentBlockStart", {}).get("start", {})
    return isinstance(start, dict) and "toolUse" in start


def _extract_answer(stream: Any) -> str:
    """Reassemble the answer from the runtime's SSE event stream.

    Only the text after the last tool call is the answer. Before searching, the
    model tends to say something like "I can't be sure of that, let me look it
    up" - read aloud, that tells the rider it does not know and then answers
    anyway, and makes a rider who cannot look away from the road listen to
    twice as much.
    """
    answer = ""
    for raw in stream.iter_lines():
        if not raw:
            continue
        line = raw.decode() if isinstance(raw, bytes) else raw
        if not line.startswith("data:"):
            continue
        try:
            event = json.loads(line[len("data:"):].strip())
        except json.JSONDecodeError:
            continue
        # The agent yields its own {"error": ...} for a rejected payload.
        if isinstance(event, dict) and "error" in event and "event" not in event:
            raise RuntimeError(str(event["error"]))
        if _is_tool_use_start(event):
            answer = ""
            continue
        delta = (
            event.get("event", {})
            .get("contentBlockDelta", {})
            .get("delta", {})
            .get("text")
        )
        if delta:
            answer += delta
    return answer.strip()


def ask(
    prompt: str, session_id: str, location: Optional[dict[str, Any]] = None
) -> str:
    """Send a prompt to the agent and return the assembled answer.

    `location` is the rider's position and heading as data, which the agent's
    place tools read instead of the model's copy of the coordinates
    (docs-parent/03_units_contracts.md UC-5).
    """
    payload: dict[str, Any] = {"question": prompt}
    if location:
        payload["location"] = location
    result = _client.invoke_agent_runtime(
        agentRuntimeArn=AGENT_ARN,
        runtimeSessionId=session_id,
        payload=json.dumps(payload).encode(),
    )
    return _extract_answer(result["response"])
