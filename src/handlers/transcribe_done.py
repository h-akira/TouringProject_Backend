"""Handler for the EventBridge "Transcribe Job State Change" event.

Where the recorded path rejoins the typed one. handlers/ask_audio.py could not
queue the question - batch transcription returns before there is a transcript -
so this runs when the transcript exists: it reads the words, settles the same
facts handlers/ask.py settles (address, heading), and puts the question on the
queue. From there handlers/worker.py cannot tell the two paths apart.

EventBridge is used rather than polling the job: the answer arrives when it
arrives, and nothing has to sit waiting for it (docs/01_architecture.md
section 7).

⚠️ The job name IS the requestId (ask_audio.py sets it), which is what ties the
event back to the record.
"""

import json
import os
from typing import Any, Optional

import boto3

from lib import prompt as prompt_builder
from lib import store

QUEUE_URL = os.environ.get("QUEUE_URL", "")
AUDIO_BUCKET = os.environ.get("AUDIO_BUCKET", "")

# Created once per container so warm invocations skip client setup.
_s3 = boto3.client("s3")
_sqs = boto3.client("sqs")


def _read_transcript(job_name: str) -> Optional[str]:
    """Pull the recognised text out of the transcript Transcribe wrote.

    The output is JSON in the same bucket the audio went to (ask_audio.py sets
    OutputKey), so it is read directly rather than through the presigned URL
    the API hands back - no expiry to race, and no second service to depend on.
    """
    key = f"transcripts/{job_name}.json"
    try:
        body = _s3.get_object(Bucket=AUDIO_BUCKET, Key=key)["Body"].read()
        payload = json.loads(body)
    except Exception as error:  # noqa: BLE001 - one shape for the rider
        print(f"failed to read transcript: {type(error).__name__}: {error}")
        return None

    transcripts = (payload.get("results") or {}).get("transcripts") or []
    if not transcripts:
        return None
    text = transcripts[0].get("transcript")
    return text.strip() if isinstance(text, str) else None


def _queue(request_id: str) -> None:
    _sqs.send_message(
        QueueUrl=QUEUE_URL,
        MessageBody=json.dumps({"requestId": request_id}),
    )


def _process(job_name: str, status: str) -> None:
    record = store.get(job_name)
    if record is None:
        # The record expired, or this event belongs to something else entirely.
        print(f"skipping {job_name}: no such request")
        return

    if status != "COMPLETED":
        # FAILED is the other status the rule subscribes to. The rider is told
        # rather than left polling until the app gives up.
        print(f"transcription {status} for {job_name}")
        store.save_error(job_name, "The recording could not be understood.")
        return

    question = _read_transcript(job_name)
    if not question:
        # A recording of silence transcribes to nothing. Asking the agent about
        # an empty question would cost a call and answer nothing.
        store.save_error(job_name, "Nothing could be heard in the recording.")
        return

    # ⚠️ Back to int/float first. DynamoDB returns numbers as Decimal, and
    # lib/prompt.py guards on isinstance(x, (int, float)) - a Decimal reads as
    # absent there, so the address and heading would vanish from every spoken
    # question with nothing raised to say so.
    location = store.from_dynamo_numbers(record.get("location") or {})
    prompt = prompt_builder.build(
        question,
        location.get("start"),
        location.get("end"),
        location.get("elapsedSeconds"),
    )

    try:
        store.start_pending(job_name, prompt)
        _queue(job_name)
    except Exception as error:  # noqa: BLE001 - one shape for the rider
        print(f"failed to queue transcribed question: {type(error).__name__}: {error}")
        store.save_error(job_name, "The question could not be accepted.")
        return

    # The transcript itself is not logged: it is what the rider said, and it
    # names where they are.
    print(f"queued {job_name}: {len(question)} chars transcribed")


def handler(event: dict[str, Any], _context: Any) -> None:
    """Handle one job-state-change event.

    Exceptions are not re-raised. A failure is already recorded on the item, and
    EventBridge would otherwise retry an event whose work is done.
    """
    detail = event.get("detail") or {}
    job_name = detail.get("TranscriptionJobName")
    status = detail.get("TranscriptionJobStatus")

    if not isinstance(job_name, str) or not job_name:
        print("skipping event: no TranscriptionJobName")
        return
    if not isinstance(status, str) or not status:
        print("skipping event: no TranscriptionJobStatus")
        return

    _process(job_name, status)
