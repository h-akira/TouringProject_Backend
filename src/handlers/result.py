"""Handler for GET /ask/{requestId}: reports on a queued question.

This is what the app polls while it waits. It only reads the table - it never
calls the agent, so it stays fast however long the answer takes.

`pending` and `processing` are both reported as "pending": the distinction
exists to make duplicate deliveries safe (docs/03_dynamodb_table.md section 4)
and means nothing to the app, which either has an answer or does not.

The exception is a `processing` record whose worker died without recording
anything and whose retries are exhausted. Nothing will move it again, so
reporting it as pending would leave the app polling until it times out. It is
reported as an error instead - the rider gets told, rather than left waiting.
"""

import json
import os
import time
from decimal import Decimal
from typing import Any, Optional

import boto3
from botocore.config import Config

from lib import store

# Past this, a `processing` record has been abandoned: the queue's retries
# (3 x 180s visibility) are spent, so nothing is coming back for it.
ABANDONED_AFTER_SECONDS = 900

# Past this, a `transcribing` record is not going to be transcribed. Generous
# next to the app's 120s polling cutoff - the app has stopped watching long
# before - so this only decides what a later poll on the same id is told.
STUCK_TRANSCRIBING_SECONDS = 600

# Long enough to start playing an answer, short enough that a leaked link is
# worth little. The app fetches it immediately.
AUDIO_URL_TTL_SECONDS = 300

AUDIO_BUCKET = os.environ.get("AUDIO_BUCKET", "")

# Created once per container so warm invocations skip client setup.
#
# ⚠️ The region is explicit, and so is the addressing style. Without them boto3
# signs against the global endpoint (s3.amazonaws.com), and S3 answers the
# fetch with a 307 to the regional host instead of the audio. curl -L survives
# that; a media player asked to stream the URL may not, and the failure would
# read as "the answer arrived but nothing plays" - measured against the real
# API, not theorised.
_s3 = boto3.client(
    "s3",
    region_name=os.environ.get("AWS_REGION", "ap-northeast-1"),
    config=Config(s3={"addressing_style": "virtual"}, signature_version="s3v4"),
)


def _response(status: int, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, ensure_ascii=False),
    }


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    request_id = (event.get("pathParameters") or {}).get("requestId")
    if not isinstance(request_id, str) or not request_id:
        return _response(400, {"error": "`requestId` is required."})

    try:
        item = store.get(request_id)
    except Exception as error:  # noqa: BLE001 - surface one shape to the client
        print(f"failed to read result: {type(error).__name__}: {error}")
        return _response(502, {"error": "The result could not be read."})

    # Also covers a requestId whose record has passed its TTL, which is
    # indistinguishable from one that never existed - and means the same thing
    # to the app either way.
    if item is None:
        return _response(404, {"error": "No such request."})

    status = item.get("status")
    session_id = item.get("sessionId", "")

    if status == "done":
        body: dict[str, Any] = {
            "status": "done",
            "answer": item.get("answer", ""),
            "sessionId": session_id,
        }
        # Absent when synthesis failed; the answer still stands and the app
        # reads it from `answer` (docs/02_api_openapi.yaml).
        audio_url = _audio_url(item.get("audioKey"))
        if audio_url:
            body["audioUrl"] = audio_url
        return _response(200, body)

    if status == "error":
        return _response(
            200,
            {
                "status": "error",
                "error": item.get("error", "The question could not be answered."),
                "sessionId": session_id,
            },
        )

    if status == "processing" and _is_abandoned(item):
        return _response(
            200,
            {
                "status": "error",
                "error": "The question could not be answered.",
                "sessionId": session_id,
            },
        )

    # ⚠️ A recording whose transcription never reported back. EventBridge is
    # the only thing that moves a `transcribing` record, so if that event never
    # arrives - the job died, or the rule failed to deliver - nothing else
    # will, and the app would poll until it gave up and call it slow rather
    # than broken.
    if status == "transcribing" and _is_stuck_transcribing(item):
        return _response(
            200,
            {
                "status": "error",
                "error": "The recording could not be understood.",
                "sessionId": session_id,
            },
        )

    return _response(200, {"status": "pending", "sessionId": session_id})


def _audio_url(audio_key: Any) -> Optional[str]:
    """Sign a short-lived URL for the answer's audio.

    The link is what the app fetches instead of receiving the bytes inline: the
    text arrives without waiting on the download, and the polled response stays
    small enough to re-send when the rider loses signal
    (docs/01_architecture.md section 7).

    Minutes, not hours: the app plays the answer as soon as it has it, so the
    link only has to outlive one playback. Returns None on failure - losing the
    audio is not worth failing the answer over.
    """
    if not isinstance(audio_key, str) or not audio_key or not AUDIO_BUCKET:
        return None
    try:
        return _s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": AUDIO_BUCKET, "Key": audio_key},
            ExpiresIn=AUDIO_URL_TTL_SECONDS,
        )
    except Exception as error:  # noqa: BLE001 - the answer still stands
        print(f"failed to sign audio url: {type(error).__name__}: {error}")
        return None


def _seconds_since(item: dict[str, Any], field: str) -> Optional[float]:
    """How long ago `field` was written, or None if it was not.

    ⚠️ Decimal is accepted alongside int/float because that is what DynamoDB
    returns for a number - it has no float type. Guarding on (int, float)
    alone reads every stored timestamp as missing, which silently disables
    whatever the caller was about to decide.
    """
    value = item.get(field)
    if not isinstance(value, (int, float, Decimal)) or isinstance(value, bool):
        return None
    return time.time() - float(value)


def _is_stuck_transcribing(item: dict[str, Any]) -> bool:
    """Whether a recording has been transcribing longer than it plausibly could.

    Keyed off createdAt rather than claimedAt: nothing claims a record in this
    state, which is exactly the problem - the only thing that moves it is an
    EventBridge event that may never come.
    """
    elapsed = _seconds_since(item, "createdAt")
    return elapsed is not None and elapsed > STUCK_TRANSCRIBING_SECONDS


def _is_abandoned(item: dict[str, Any]) -> bool:
    """Whether a claimed question has been left unfinished for good."""
    # Absent only on records written before claimedAt existed.
    elapsed = _seconds_since(item, "claimedAt")
    return elapsed is not None and elapsed > ABANDONED_AFTER_SECONDS
