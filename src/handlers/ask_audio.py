"""Handler for POST /ask-audio: accepts a recording and starts transcribing it.

The voice counterpart of handlers/ask.py. It does the same gatekeeping - this
is the only place a recording can be checked before it costs anything - but it
cannot finish the job: batch transcription returns before there is a
transcript, so the question is not queued here. Amazon Transcribe reports
completion through EventBridge, and handlers/transcribe_done.py picks it up,
builds the prompt and queues it (docs/01_architecture.md section 7).

⚠️ Transcribe is called in batch, not streaming, because streaming is
bidirectional and cannot sit behind Lambda. Doing so would mean handing the app
AWS credentials and losing every check below (adr/002).

The response is 202 with the same requestId shape POST /ask returns, so the app
polls GET /ask/{requestId} either way.
"""

import json
import os
import uuid
from email.parser import BytesParser
from email.policy import default as default_policy
from typing import Any, Optional

import base64
import boto3

from lib import store

# What the app records (docs/01_architecture.md section 7). Transcribe prefers
# FLAC or WAV, but Android's recorder emits neither.
AUDIO_FORMAT = "m4a"
AUDIO_CONTENT_TYPES = ("audio/mp4", "audio/m4a", "audio/x-m4a")

# ⚠️ This is the recording-length limit. Audio never reaches a place where
# seconds can be counted before the cost is incurred, so size stands in for
# duration: at this encoding a question runs tens of KB, so 2MB is minutes of
# speech - far above normal use, and the point is to catch a client asking for
# trouble rather than to trim a long question.
MAX_AUDIO_BYTES = 2 * 1024 * 1024

# AgentCore rejects a runtimeSessionId below this length.
MIN_SESSION_ID_CHARS = 33

QUEUE_URL = os.environ.get("QUEUE_URL", "")
AUDIO_BUCKET = os.environ.get("AUDIO_BUCKET", "")

# ⚠️ Transcribe writes the transcript after this function has returned, so it
# cannot borrow these credentials - it assumes this role instead. Without it
# the job fails at the end, having already been paid for.
TRANSCRIBE_ROLE_ARN = os.environ.get("TRANSCRIBE_ROLE_ARN", "")

# Created once per container so warm invocations skip client setup.
_s3 = boto3.client("s3")
_transcribe = boto3.client("transcribe")


def _response(status: int, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, ensure_ascii=False),
    }


def _new_session_id() -> str:
    # uuid4().hex is 32 chars, one short of the minimum, so it is prefixed.
    return f"touring-{uuid.uuid4().hex}"


def _raw_body(event: dict[str, Any]) -> Optional[bytes]:
    """Get the request body as bytes.

    API Gateway base64-encodes a binary body, flagging it with
    isBase64Encoded - which it does here, since multipart carries the audio as
    bytes. ⚠️ The API must list multipart/form-data under BinaryMediaTypes or
    the payload arrives corrupted rather than missing, which looks like a bad
    recording rather than a configuration mistake.
    """
    body = event.get("body")
    if body is None:
        return None
    if event.get("isBase64Encoded"):
        try:
            return base64.b64decode(body)
        except Exception:  # noqa: BLE001 - malformed input, not our bug
            return None
    if isinstance(body, bytes):
        return body
    return str(body).encode("utf-8")


def _parse_multipart(event: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Split the multipart body into its parts.

    Uses the stdlib email parser rather than a dependency: multipart/form-data
    is MIME, and the Lambda has no requirements.txt to add to.

    Returns a dict of part name -> value (bytes for the file, str for the rest),
    or None if the body is not parseable as multipart.
    """
    headers = event.get("headers") or {}
    # Header casing is not guaranteed through API Gateway.
    content_type = next(
        (v for k, v in headers.items() if k.lower() == "content-type"), ""
    )
    if "multipart/form-data" not in (content_type or "").lower():
        return None

    raw = _raw_body(event)
    if not raw:
        return None

    # The parser needs the Content-Type header (it carries the boundary) in
    # front of the body, which is how it would look on the wire.
    message = BytesParser(policy=default_policy).parsebytes(
        f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode() + raw
    )
    if not message.is_multipart():
        return None

    parts: dict[str, Any] = {}
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        if name == "audio":
            parts[name] = payload
            # Kept so the handler can reject a format Transcribe would choke
            # on. Under a name no form field can collide with.
            parts["audio:content-type"] = (part.get_content_type() or "").lower()
        else:
            parts[name] = payload.decode("utf-8", errors="replace")
    return parts


def _start_transcription(job_name: str, s3_uri: str) -> None:
    """Kick off the batch job. Completion arrives via EventBridge.

    The output goes to the same bucket the audio went to, so one lifecycle rule
    covers both and neither lingers.
    """
    _transcribe.start_transcription_job(
        TranscriptionJobName=job_name,
        LanguageCode="ja-JP",
        MediaFormat=AUDIO_FORMAT,
        Media={"MediaFileUri": s3_uri},
        OutputBucketName=AUDIO_BUCKET,
        OutputKey=f"transcripts/{job_name}.json",
        JobExecutionSettings={
            "DataAccessRoleArn": TRANSCRIBE_ROLE_ARN,
        },
    )


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    if not QUEUE_URL or not AUDIO_BUCKET:
        return _response(502, {"error": "The service is not configured."})

    parts = _parse_multipart(event)
    if parts is None:
        return _response(400, {"error": "Request body must be multipart/form-data."})

    audio = parts.get("audio")
    if not isinstance(audio, bytes) or not audio:
        return _response(400, {"error": "`audio` is required and must be non-empty."})

    # ⚠️ MediaFormat is sent to Transcribe as a constant, so anything that is
    # not M4A produces a job that fails minutes later and reaches the rider as
    # "the recording could not be understood" - a submit-time mistake reported
    # as a recognition failure. Rejected here instead.
    declared = parts.get("audio:content-type", "")
    if declared and declared not in AUDIO_CONTENT_TYPES:
        return _response(
            400,
            {"error": f"`audio` must be {AUDIO_FORMAT} ({AUDIO_CONTENT_TYPES[0]})."},
        )

    # ⚠️ The check that replaces "how many seconds may you record". API Gateway
    # caps the payload at 10MB before this runs, but that ceiling is far above
    # anything a question needs, so the useful limit is here.
    if len(audio) > MAX_AUDIO_BYTES:
        return _response(
            413,
            {"error": f"`audio` must be at most {MAX_AUDIO_BYTES} bytes."},
        )

    location_raw = parts.get("location")
    if not isinstance(location_raw, str) or not location_raw.strip():
        return _response(400, {"error": "`location` is required."})
    try:
        location = json.loads(location_raw)
    except json.JSONDecodeError:
        return _response(400, {"error": "`location` must be valid JSON."})
    if not isinstance(location, dict):
        return _response(400, {"error": "`location` must be a JSON object."})

    session_id = parts.get("sessionId")
    if session_id is None:
        session_id = _new_session_id()
    elif not isinstance(session_id, str) or len(session_id) < MIN_SESSION_ID_CHARS:
        return _response(
            400,
            {"error": f"`sessionId` must be at least {MIN_SESSION_ID_CHARS} characters."},
        )

    # The request id doubles as the transcription job name, so the completion
    # event can be traced back to this record without a lookup table.
    request_id = str(uuid.uuid4())
    key = f"audio/{request_id}.{AUDIO_FORMAT}"

    try:
        _s3.put_object(
            Bucket=AUDIO_BUCKET,
            Key=key,
            Body=audio,
            ContentType=AUDIO_CONTENT_TYPES[0],
        )
        # Stored before the job starts so the completion handler always finds a
        # record; the location rides along because the prompt cannot be built
        # until there is a transcript to put in it.
        store.create_pending_audio(request_id, session_id, location)
        _start_transcription(request_id, f"s3://{AUDIO_BUCKET}/{key}")
    except Exception as error:  # noqa: BLE001 - surface one shape to the client
        # Logged for CloudWatch; the client gets a generic message rather than
        # the raw AWS error, which can name internal resources.
        print(f"failed to accept recording: {type(error).__name__}: {error}")
        # ⚠️ The record may already exist by now - create_pending_audio runs
        # before the job starts, so a failure to start it leaves a
        # `transcribing` record that no completion event will ever move. Mark
        # it failed rather than letting it sit there until its TTL.
        try:
            store.save_error(request_id, "The recording could not be accepted.")
        except Exception:  # noqa: BLE001 - nothing left to try
            pass
        return _response(502, {"error": "The recording could not be accepted."})

    print(f"transcribing {request_id}: {len(audio)} bytes")

    # 202: accepted, not answered - same shape POST /ask returns.
    return _response(202, {"requestId": request_id, "sessionId": session_id})
