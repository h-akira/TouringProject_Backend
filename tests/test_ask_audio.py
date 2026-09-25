"""Tests for the POST /ask-audio handler.

This handler is the only place a recording can be checked before it costs
anything, so most of these are about what it refuses. The rest cover the shape
it hands onward: the request id doubles as the transcription job name, and the
position is stored because the prompt cannot be built until the transcript
exists.

S3, Transcribe and DynamoDB are stubbed - what matters is what gets sent, not
the wire format.
"""

import base64
import importlib
import json

import pytest

BOUNDARY = "----testboundary"


def _multipart(audio=b"fake-m4a-bytes", location=None, session_id=None, **extra):
    """Build a multipart body the way the app would."""
    parts = []
    if audio is not None:
        parts.append(
            b'--' + BOUNDARY.encode() + b'\r\n'
            b'Content-Disposition: form-data; name="audio"; filename="q.m4a"\r\n'
            b'Content-Type: audio/mp4\r\n\r\n' + audio + b'\r\n'
        )
    if location is not None:
        parts.append(
            b'--' + BOUNDARY.encode() + b'\r\n'
            b'Content-Disposition: form-data; name="location"\r\n\r\n'
            + location.encode() + b'\r\n'
        )
    if session_id is not None:
        parts.append(
            b'--' + BOUNDARY.encode() + b'\r\n'
            b'Content-Disposition: form-data; name="sessionId"\r\n\r\n'
            + session_id.encode() + b'\r\n'
        )
    parts.append(b'--' + BOUNDARY.encode() + b'--\r\n')
    return b"".join(parts)


def _event(body=None, content_type=None, **kwargs):
    if body is None:
        body = _multipart(location=json.dumps({"start": {"latitude": 35.0, "longitude": 139.0}}))
    return {
        "headers": {
            "Content-Type": content_type
            or f"multipart/form-data; boundary={BOUNDARY}"
        },
        "body": base64.b64encode(body).decode(),
        "isBase64Encoded": True,
        **kwargs,
    }


@pytest.fixture
def ask_audio(monkeypatch):
    """Import the handler with the bucket and queue configured."""
    monkeypatch.setenv("QUEUE_URL", "https://sqs.example/queue")
    monkeypatch.setenv("AUDIO_BUCKET", "bucket-test")
    monkeypatch.setenv("TRANSCRIBE_ROLE_ARN", "arn:aws:iam::000000000000:role/test")
    module = importlib.import_module("handlers.ask_audio")
    importlib.reload(module)
    return module


def _call(ask_audio, event=None, capture=None, fail_on=None):
    def fake_put_object(**kwargs):
        if fail_on == "s3":
            raise RuntimeError("bucket unavailable")
        if capture is not None:
            capture["put"] = kwargs

    def fake_create_pending_audio(request_id, session_id, location):
        if capture is not None:
            capture.update(
                {
                    "requestId": request_id,
                    "sessionId": session_id,
                    "location": location,
                }
            )

    def fake_start_transcription_job(**kwargs):
        if fail_on == "transcribe":
            raise RuntimeError("transcribe unavailable")
        if capture is not None:
            capture["job"] = kwargs

    ask_audio._s3.put_object = fake_put_object
    ask_audio.store.create_pending_audio = fake_create_pending_audio
    ask_audio._transcribe.start_transcription_job = fake_start_transcription_job
    return ask_audio.handler(event if event is not None else _event(), None)


def test_recording_is_accepted_not_answered(ask_audio):
    capture: dict = {}
    result = _call(ask_audio, capture=capture)
    body = json.loads(result["body"])

    # 202 with the same shape POST /ask returns, so the app polls either way.
    assert result["statusCode"] == 202
    assert body["requestId"]
    assert "answer" not in body
    assert len(body["sessionId"]) >= ask_audio.MIN_SESSION_ID_CHARS


def test_job_name_is_the_request_id(ask_audio):
    # This is what lets the completion event find the record without a lookup.
    capture: dict = {}
    result = _call(ask_audio, capture=capture)

    request_id = json.loads(result["body"])["requestId"]
    assert capture["job"]["TranscriptionJobName"] == request_id
    assert capture["requestId"] == request_id


def test_audio_is_stored_before_the_job_starts(ask_audio):
    # The job reads from S3, so the object has to be there first.
    capture: dict = {}
    result = _call(ask_audio, capture=capture)

    request_id = json.loads(result["body"])["requestId"]
    assert capture["put"]["Key"] == f"audio/{request_id}.m4a"
    assert capture["put"]["Body"] == b"fake-m4a-bytes"
    assert capture["job"]["Media"]["MediaFileUri"].endswith(capture["put"]["Key"])


def test_japanese_is_requested(ask_audio):
    # The whole app is Japanese; the default would be English.
    capture: dict = {}
    _call(ask_audio, capture=capture)

    assert capture["job"]["LanguageCode"] == "ja-JP"
    assert capture["job"]["MediaFormat"] == "m4a"


def test_position_is_stored_for_the_completion_handler(ask_audio):
    # The prompt cannot be built yet - there is no transcript - so the position
    # has to survive until there is one.
    capture: dict = {}
    location = {
        "start": {"latitude": 35.0, "longitude": 139.0},
        "end": {"latitude": 34.9, "longitude": 139.0},
        "elapsedSeconds": 120,
    }
    _call(
        ask_audio,
        event=_event(_multipart(location=json.dumps(location))),
        capture=capture,
    )

    assert capture["location"] == location


def test_supplied_session_id_is_passed_through(ask_audio):
    session_id = "touring-" + "a" * 32
    capture: dict = {}
    result = _call(
        ask_audio,
        event=_event(
            _multipart(
                location=json.dumps({"start": {"latitude": 35.0, "longitude": 139.0}}),
                session_id=session_id,
            )
        ),
        capture=capture,
    )

    assert capture["sessionId"] == session_id
    assert json.loads(result["body"])["sessionId"] == session_id


def test_oversized_recording_is_refused(ask_audio):
    # ⚠️ The check that replaces "how many seconds may you record". Audio never
    # reaches anywhere its duration could be counted before it costs money.
    oversized = b"x" * (ask_audio.MAX_AUDIO_BYTES + 1)
    result = _call(
        ask_audio,
        event=_event(
            _multipart(
                audio=oversized,
                location=json.dumps({"start": {"latitude": 35.0, "longitude": 139.0}}),
            )
        ),
    )

    assert result["statusCode"] == 413


def test_oversized_recording_never_reaches_s3(ask_audio):
    # Refusing after the upload would mean paying to store what we rejected,
    # and paying Transcribe to read it.
    capture: dict = {}
    _call(
        ask_audio,
        event=_event(
            _multipart(
                audio=b"x" * (ask_audio.MAX_AUDIO_BYTES + 1),
                location=json.dumps({"start": {"latitude": 35.0, "longitude": 139.0}}),
            )
        ),
        capture=capture,
    )

    assert "put" not in capture
    assert "job" not in capture


@pytest.mark.parametrize(
    "event",
    [
        # Not multipart at all.
        _event(b'{"question":"text"}', content_type="application/json"),
        # No audio part.
        _event(_multipart(audio=None, location='{"start":{"latitude":35,"longitude":139}}')),
        # Empty recording: the button was pressed and released.
        _event(_multipart(audio=b"", location='{"start":{"latitude":35,"longitude":139}}')),
        # No location.
        _event(_multipart()),
        # Location that is not JSON.
        _event(_multipart(location="not json")),
        # Location that is JSON but not an object.
        _event(_multipart(location="[1,2]")),
        # Session id too short for AgentCore.
        _event(
            _multipart(
                location='{"start":{"latitude":35,"longitude":139}}', session_id="short"
            )
        ),
    ],
)
def test_invalid_requests_are_rejected(ask_audio, event):
    result = _call(ask_audio, event=event)
    assert result["statusCode"] == 400


def test_storage_failure_is_reported_without_naming_resources(ask_audio):
    result = _call(ask_audio, fail_on="s3")
    body = json.loads(result["body"])

    assert result["statusCode"] == 502
    # The rider is told it failed, not which bucket.
    assert "bucket" not in body["error"].lower()


def test_transcribe_failure_is_reported(ask_audio):
    result = _call(ask_audio, fail_on="transcribe")

    assert result["statusCode"] == 502


def test_missing_configuration_does_not_pretend_to_work(ask_audio, monkeypatch):
    # A recording accepted into a stack with no bucket would be lost silently.
    monkeypatch.setenv("AUDIO_BUCKET", "")
    module = importlib.reload(ask_audio)

    result = module.handler(_event(), None)
    assert result["statusCode"] == 502


def test_transcribe_is_given_the_role_it_writes_with(ask_audio):
    # ⚠️ Transcribe writes the transcript after this function has returned, so
    # it cannot use the Lambda's credentials. Without the role the job runs and
    # then fails at the last step, having already been billed.
    capture: dict = {}
    _call(ask_audio, capture=capture)

    settings = capture["job"]["JobExecutionSettings"]
    assert settings["DataAccessRoleArn"] == ask_audio.TRANSCRIBE_ROLE_ARN


def test_the_transcript_is_written_to_our_own_bucket(ask_audio):
    # Not the service-managed bucket: the transcript is the rider speaking, and
    # there it could be neither expired by our lifecycle rule nor deleted
    # without a support case.
    capture: dict = {}
    result = _call(ask_audio, capture=capture)

    request_id = json.loads(result["body"])["requestId"]
    assert capture["job"]["OutputBucketName"] == "bucket-test"
    assert capture["job"]["OutputKey"] == f"transcripts/{request_id}.json"


def test_a_format_transcribe_cannot_read_is_rejected_at_submit(ask_audio):
    # MediaFormat is sent as a constant, so a WAV upload would produce a job
    # that fails minutes later and reaches the rider as "could not be
    # understood" - a submit-time mistake dressed up as a recognition failure.
    body = (
        b'--' + BOUNDARY.encode() + b'\r\n'
        b'Content-Disposition: form-data; name="audio"; filename="q.wav"\r\n'
        b'Content-Type: audio/wav\r\n\r\nRIFF....\r\n'
        b'--' + BOUNDARY.encode() + b'\r\n'
        b'Content-Disposition: form-data; name="location"\r\n\r\n'
        b'{"start":{"latitude":35.0,"longitude":139.0}}\r\n'
        b'--' + BOUNDARY.encode() + b'--\r\n'
    )
    capture: dict = {}
    result = _call(ask_audio, event=_event(body), capture=capture)

    assert result["statusCode"] == 400
    # And it costs nothing: no upload, no job.
    assert "put" not in capture and "job" not in capture


def test_a_failure_starting_the_job_does_not_strand_the_record(ask_audio):
    # create_pending_audio runs before the job starts, so a failure here leaves
    # a `transcribing` record that no completion event will ever move.
    saved = {}

    def fake_save_error(request_id, message):
        saved["requestId"] = request_id
        saved["message"] = message

    ask_audio.store.save_error = fake_save_error
    result = _call(ask_audio, fail_on="transcribe")

    assert result["statusCode"] == 502
    assert saved["requestId"]
