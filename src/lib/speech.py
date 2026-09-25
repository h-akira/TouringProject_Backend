"""Amazon Polly: turns the answer into something the rider can hear.

Synthesis happens as soon as the answer exists (handlers/worker.py) rather than
when the app asks for it. Generating on demand would put Polly's latency in
front of the first playback, which is the moment the rider is waiting on
(docs/01_architecture.md section 7).

The audio lands in the same bucket the recording went to, so one lifecycle rule
covers both. handlers/result.py hands back a presigned URL to it.
"""

import os
from typing import Optional

import boto3

AUDIO_BUCKET = os.environ.get("AUDIO_BUCKET", "")

# ⚠️ Japanese has four Polly voices but only three do neural: Kazuha and
# Tomoko (female) and Takumi (male). Mizuki is standard-only, so picking her
# would cost audible quality (confirmed with describe-voices, not remembered).
VOICE_ID = os.environ.get("POLLY_VOICE_ID", "Kazuha")
ENGINE = "neural"

# MP3 rather than PCM: it is what a phone plays without help, and it keeps the
# object small enough to fetch over a patchy connection.
OUTPUT_FORMAT = "mp3"
CONTENT_TYPE = "audio/mpeg"

# Created once per container so warm invocations skip client setup.
_polly = boto3.client("polly")
_s3 = boto3.client("s3")


def audio_key(request_id: str) -> str:
    return f"answers/{request_id}.{OUTPUT_FORMAT}"


def synthesize(request_id: str, text: str) -> Optional[str]:
    """Render `text` to speech and store it. Returns the S3 key, or None.

    Returning None rather than raising is deliberate: the answer already exists
    and is worth delivering. A rider who gets text without audio has lost the
    convenience, not the answer, so a synthesis failure must not fail the
    question (handlers/worker.py stores the answer either way).
    """
    if not AUDIO_BUCKET or not text:
        return None

    try:
        response = _polly.synthesize_speech(
            Text=text,
            OutputFormat=OUTPUT_FORMAT,
            VoiceId=VOICE_ID,
            Engine=ENGINE,
            LanguageCode="ja-JP",
        )
        audio = response["AudioStream"].read()
    except Exception as error:  # noqa: BLE001 - the answer still stands
        print(f"synthesize_speech failed: {type(error).__name__}: {error}")
        return None

    key = audio_key(request_id)
    try:
        _s3.put_object(
            Bucket=AUDIO_BUCKET, Key=key, Body=audio, ContentType=CONTENT_TYPE
        )
    except Exception as error:  # noqa: BLE001 - the answer still stands
        print(f"failed to store answer audio: {type(error).__name__}: {error}")
        return None

    return key
