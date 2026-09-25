"""Handler for the SQS queue: calls the agent and stores the answer.

This is where the waiting happens. Invoked from the queue rather than API
Gateway, so the agent's 10-25s is no longer racing the 29s request ceiling
(docs/01a_async_ask.md).

⚠️ SQS delivers at least once, so this handler must tolerate being run twice
for the same question - AWS is explicit about that requirement. Here a repeat
would call the agent, and be billed, a second time, so the first thing it does
is claim the record; a duplicate loses the claim and returns without calling
anything. See docs/03_dynamodb_table.md section 4.
"""

import json
from typing import Any

from lib import agent, speech, store


def _process(request_id: str) -> None:
    record = store.claim(request_id)
    if record is None:
        # Either a duplicate delivery or a retry of something already finished.
        print(f"skipping {request_id}: not claimable")
        return

    prompt = record.get("prompt")
    session_id = record.get("sessionId")
    if not prompt or not session_id:
        # Should not happen: create_pending writes both.
        store.save_error(request_id, "The question was incomplete.")
        return

    try:
        answer = agent.ask(str(prompt), str(session_id))
    except Exception as error:  # noqa: BLE001 - the rider gets one shape
        # Logged for CloudWatch; the stored message is what the app shows, so
        # it must not name internal resources.
        print(f"invoke_agent_runtime failed: {type(error).__name__}: {error}")
        store.save_error(request_id, "The agent could not be reached.")
        return

    if not answer:
        store.save_error(request_id, "The agent returned no answer.")
        return

    # Synthesised now rather than when the app asks, so playback does not wait
    # on Polly. A failure here returns None and the answer is stored without
    # audio: the rider reads it instead of hearing it (lib/speech.py).
    audio_key = speech.synthesize(request_id, answer)

    store.save_answer(request_id, answer, audio_key)
    print(f"answered {request_id}: {len(answer)} chars audio={bool(audio_key)}")


def handler(event: dict[str, Any], _context: Any) -> None:
    """Process the queued question(s).

    BatchSize is 1 (see template.yaml), so there is normally a single record,
    but the loop keeps this correct if that ever changes.

    Exceptions are deliberately not re-raised: a failure is already recorded on
    the item, and letting it propagate would send the message back for a retry
    that would call the agent again. The message is only left on the queue when
    something goes wrong before the failure could be stored.
    """
    for record in event.get("Records", []):
        try:
            body = json.loads(record.get("body") or "{}")
        except json.JSONDecodeError:
            print("skipping record: body is not valid JSON")
            continue

        request_id = body.get("requestId")
        if not isinstance(request_id, str) or not request_id:
            print("skipping record: no requestId")
            continue

        _process(request_id)
