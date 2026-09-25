"""DynamoDB access for the async /ask flow.

One table holds every entity type, keyed by pk/sk - see
docs/03_dynamodb_table.md. This module owns the "ASK#" records: the status of a
question between the app posting it and collecting the answer.

Records expire after an hour (same doc, section 4). Coordinates do not appear
in them today, but only because the prompt is built before anything is stored -
it is not a rule this module enforces.
"""

import os
import time
from decimal import Decimal
from typing import Any, Optional

import boto3
from botocore.exceptions import ClientError

TABLE_NAME = os.environ.get("TABLE_NAME", "")

# Long enough that a poll can never outlive the record (the app gives up after
# 120s), short enough that answers naming the rider's location do not linger.
TTL_SECONDS = 3600

# When a claim is old enough that its holder cannot still be running, and so may
# be taken over. Must exceed the queue's visibility timeout (180s in
# template.yaml) - below that, a worker still doing its job could have the
# question taken off it, and the agent would be called twice.
CLAIM_STALE_SECONDS = 300

# Created once per container so warm invocations skip client setup.
_resource = boto3.resource("dynamodb")


def _table() -> Any:
    return _resource.Table(TABLE_NAME)


def _key(request_id: str) -> dict[str, str]:
    return {"pk": f"ASK#{request_id}", "sk": "STATUS"}


def create_pending(request_id: str, session_id: str, prompt: str) -> None:
    """Record a question as accepted, before anything has been generated.

    The prompt is stored because the worker runs in a separate invocation and
    needs it; it already has the address folded in, so the coordinates
    themselves never have to be written.
    """
    now = int(time.time())
    _table().put_item(
        Item={
            **_key(request_id),
            "status": "pending",
            "sessionId": session_id,
            "prompt": prompt,
            "createdAt": now,
            "expiresAt": now + TTL_SECONDS,
        }
    )


def _to_dynamo_numbers(value: Any) -> Any:
    """Convert floats to Decimal, which is the only number DynamoDB takes.

    ⚠️ boto3's resource layer raises TypeError("Float types are not supported")
    rather than rounding, so coordinates arriving from json.loads - which are
    always float - would fail the write outright.

    Applied on the way in, so callers can hand over ordinary JSON.
    lib/prompt.py sees floats again on the way out (see from_dynamo_numbers).
    """
    if isinstance(value, float):
        # Via str, not float->Decimal directly: Decimal(35.68) carries the
        # binary representation's noise into the stored value.
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: _to_dynamo_numbers(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_to_dynamo_numbers(v) for v in value]
    return value


def from_dynamo_numbers(value: Any) -> Any:
    """Undo _to_dynamo_numbers: Decimal back to int/float.

    ⚠️ Needed because Decimal is not a float. Code that guards on
    `isinstance(x, (int, float))` - lib/prompt.py does, deliberately - silently
    treats a Decimal as absent, which would drop the address and heading from
    every spoken question without raising anything.
    """
    if isinstance(value, Decimal):
        # Whole numbers came in as int (elapsedSeconds); keep them that way, or
        # the int-only guards reject them just as float ones would.
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: from_dynamo_numbers(v) for k, v in value.items()}
    if isinstance(value, list):
        return [from_dynamo_numbers(v) for v in value]
    return value


def create_pending_audio(
    request_id: str, session_id: str, location: dict[str, Any]
) -> None:
    """Record a recorded question, before it has been transcribed.

    The typed path stores a finished prompt, because it has one by the time it
    writes. This path does not: transcription has only just started, and the
    prompt cannot be built without the transcript. So the position is stored
    instead, and handlers/transcribe_done.py builds the prompt when the words
    arrive.

    ⚠️ This is the one record that holds raw coordinates. They are what the
    address gets resolved from later, and the record expires with everything
    else (TTL_SECONDS).
    """
    now = int(time.time())
    _table().put_item(
        Item={
            **_key(request_id),
            "status": "transcribing",
            "sessionId": session_id,
            "location": _to_dynamo_numbers(location),
            "createdAt": now,
            "expiresAt": now + TTL_SECONDS,
        }
    )


def start_pending(request_id: str, prompt: str) -> None:
    """Move a transcribed question into the queue-able state.

    Mirrors what create_pending writes for a typed question: once the prompt
    exists the two paths are indistinguishable, so the worker needs no notion
    of where the question came from. The coordinates go at the same time - they
    have served their purpose, and the prompt carries the address instead.
    """
    _table().update_item(
        Key=_key(request_id),
        UpdateExpression="SET #s = :pending, prompt = :prompt REMOVE #loc",
        ExpressionAttributeNames={"#s": "status", "#loc": "location"},
        ExpressionAttributeValues={":pending": "pending", ":prompt": prompt},
    )


def claim(request_id: str) -> Optional[dict[str, Any]]:
    """Take ownership of a question, or return None if someone already has it.

    SQS delivers at least once, so the same question can arrive twice; without
    this the agent would be called - and billed - twice over. The conditional
    write is what makes that safe: only one caller can move a record out of
    `pending`, and the loser returns None and stops.

    A claim can also be taken over once it has gone stale. A worker killed
    mid-flight - a Lambda timeout leaves no chance to record anything - would
    otherwise strand the record in `processing`, where it reads as "still
    working" to the app until it gives up. Past CLAIM_STALE_SECONDS the record
    is treated as abandoned, which is safe because the queue's visibility
    timeout has expired by then: whoever held it is no longer running.
    """
    now = int(time.time())
    try:
        result = _table().update_item(
            Key=_key(request_id),
            UpdateExpression="SET #s = :processing, claimedAt = :now",
            # Fresh work, or work whose owner has demonstrably stopped.
            ConditionExpression=(
                "#s = :pending OR (#s = :processing AND claimedAt < :stale)"
            ),
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":processing": "processing",
                ":pending": "pending",
                ":now": now,
                ":stale": now - CLAIM_STALE_SECONDS,
            },
            ReturnValues="ALL_NEW",
        )
    except ClientError as error:
        if error.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return None
        raise
    return result.get("Attributes")


def save_answer(
    request_id: str, answer: str, audio_key: Optional[str] = None
) -> None:
    """Record the answer, and where its audio landed if there is any.

    `audio_key` is optional because synthesis is allowed to fail without taking
    the answer with it - the record then holds text alone, and the app reads it
    out of `answer` (lib/speech.py).
    """
    expression = "SET #s = :done, answer = :answer"
    names = {"#s": "status"}
    values: dict[str, Any] = {":done": "done", ":answer": answer}

    if audio_key:
        expression += ", audioKey = :audioKey"
        values[":audioKey"] = audio_key

    _table().update_item(
        Key=_key(request_id),
        UpdateExpression=f"{expression} REMOVE prompt",
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
    )


def save_error(request_id: str, message: str) -> None:
    """Record a failure. `message` is shown to the rider, so keep it generic."""
    _table().update_item(
        Key=_key(request_id),
        UpdateExpression="SET #s = :error, #e = :message REMOVE prompt",
        ExpressionAttributeNames={"#s": "status", "#e": "error"},
        ExpressionAttributeValues={":error": "error", ":message": message},
    )


def get(request_id: str) -> Optional[dict[str, Any]]:
    return _table().get_item(Key=_key(request_id)).get("Item")
