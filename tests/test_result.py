"""Tests for GET /ask/{requestId}, which the app polls while it waits.

The contract matters more than it looks: the app decides whether to keep
polling from `status`, so anything ambiguous here turns into a client that
either gives up early or never stops.
"""

import importlib
import json
import time
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

SESSION_ID = "touring-" + "a" * 32


@pytest.fixture
def result(monkeypatch):
    monkeypatch.setenv("AUDIO_BUCKET", "bucket-test")
    module = importlib.import_module("handlers.result")
    importlib.reload(module)
    module._s3.generate_presigned_url = (
        lambda _op, Params=None, ExpiresIn=None: f"https://signed/{Params['Key']}"
    )
    return module


def _call(result, item, request_id="req-1", raises=None):
    def fake_get(_request_id):
        if raises is not None:
            raise raises
        return item

    result.store.get = fake_get
    return result.handler({"pathParameters": {"requestId": request_id}}, None)


def test_pending_is_reported_while_the_agent_works(result):
    response = _call(result, {"status": "pending", "sessionId": SESSION_ID})
    body = json.loads(response["body"])

    assert response["statusCode"] == 200
    assert body["status"] == "pending"
    assert "answer" not in body


def test_processing_looks_the_same_as_pending_to_the_app(result):
    # `processing` exists only to make duplicate deliveries safe; the app has
    # no use for the distinction and should keep polling either way.
    response = _call(result, {"status": "processing", "sessionId": SESSION_ID})

    assert json.loads(response["body"])["status"] == "pending"


def test_done_returns_the_answer(result):
    response = _call(
        result,
        {"status": "done", "answer": "それは富士山です", "sessionId": SESSION_ID},
    )
    body = json.loads(response["body"])

    assert body["status"] == "done"
    assert body["answer"] == "それは富士山です"
    # Echoed so the app can keep the conversation going.
    assert body["sessionId"] == SESSION_ID


def test_error_is_reported_as_a_200_with_a_status(result):
    # Not an HTTP error: the request itself succeeded, and the app needs to
    # read the status to know to stop polling.
    response = _call(
        result,
        {"status": "error", "error": "The agent could not be reached.", "sessionId": SESSION_ID},
    )
    body = json.loads(response["body"])

    assert response["statusCode"] == 200
    assert body["status"] == "error"
    assert body["error"]


def test_an_abandoned_question_is_reported_as_an_error(result):
    # A worker killed by a timeout records nothing, and once the queue's
    # retries are spent nothing will move the record again. Reporting it as
    # pending would leave the rider watching a spinner until it gives up.
    response = _call(
        result,
        {
            "status": "processing",
            "sessionId": SESSION_ID,
            "claimedAt": time.time() - result.ABANDONED_AFTER_SECONDS - 1,
        },
    )

    assert json.loads(response["body"])["status"] == "error"


def test_a_recently_claimed_question_is_still_pending(result):
    # The other side of that cutoff: work in progress must not be called dead.
    response = _call(
        result,
        {"status": "processing", "sessionId": SESSION_ID, "claimedAt": time.time()},
    )

    assert json.loads(response["body"])["status"] == "pending"


def test_processing_without_a_claim_time_is_pending(result):
    # Records written before claimedAt existed; treat them as in progress
    # rather than declaring them dead on no evidence.
    response = _call(result, {"status": "processing", "sessionId": SESSION_ID})

    assert json.loads(response["body"])["status"] == "pending"


def test_unknown_request_is_404(result):
    # Also what an expired record looks like, which means the same thing here.
    response = _call(result, None)
    assert response["statusCode"] == 404


def test_missing_request_id_is_rejected(result):
    response = result.handler({"pathParameters": None}, None)
    assert response["statusCode"] == 400


def test_read_failure_does_not_leak_details(result):
    response = _call(
        result,
        None,
        raises=RuntimeError("arn:aws:iam::123456789012:table/secret denied"),
    )

    assert response["statusCode"] == 502
    assert "123456789012" not in response["body"]


def test_answer_audio_comes_back_as_a_link(result):
    # Not the bytes: the text has to arrive without waiting on the download,
    # and this response is re-sent whenever the rider loses signal.
    response = _call(
        result,
        {
            "status": "done",
            "answer": "それは富士山です",
            "audioKey": "answers/req-1.mp3",
            "sessionId": SESSION_ID,
        },
    )
    body = json.loads(response["body"])

    assert body["audioUrl"] == "https://signed/answers/req-1.mp3"
    assert body["answer"] == "それは富士山です"


def test_answer_without_audio_is_still_delivered(result):
    # Synthesis is allowed to fail on its own; the rider reads the answer.
    response = _call(
        result,
        {"status": "done", "answer": "それは富士山です", "sessionId": SESSION_ID},
    )
    body = json.loads(response["body"])

    assert body["status"] == "done"
    assert body["answer"] == "それは富士山です"
    assert "audioUrl" not in body


def test_signing_failure_does_not_fail_the_answer(result):
    def boom(*_args, **_kwargs):
        raise RuntimeError("no such bucket")

    result._s3.generate_presigned_url = boom
    response = _call(
        result,
        {
            "status": "done",
            "answer": "それは富士山です",
            "audioKey": "answers/req-1.mp3",
            "sessionId": SESSION_ID,
        },
    )
    body = json.loads(response["body"])

    assert body["answer"] == "それは富士山です"
    assert "audioUrl" not in body


def test_a_recording_being_transcribed_reads_as_pending(result):
    # ⚠️ The app knows three statuses. `transcribing` is an internal step, so it
    # must present as pending or the app would treat it as unknown and stop.
    response = _call(result, {"status": "transcribing", "sessionId": SESSION_ID})
    body = json.loads(response["body"])

    assert body["status"] == "pending"


def test_a_transcription_that_never_reported_back_becomes_an_error(result):
    # ⚠️ EventBridge is the only thing that moves a `transcribing` record. If
    # that event never arrives, nothing else will, and the app would poll until
    # it gave up - reading as slow rather than broken.
    response = _call(
        result,
        {
            "status": "transcribing",
            "sessionId": SESSION_ID,
            "createdAt": time.time() - result.STUCK_TRANSCRIBING_SECONDS - 1,
        },
    )

    assert json.loads(response["body"])["status"] == "error"


def test_a_recent_transcription_is_still_pending(result):
    # The other side of that cutoff: a job still running must not be called dead.
    response = _call(
        result,
        {"status": "transcribing", "sessionId": SESSION_ID, "createdAt": time.time()},
    )

    assert json.loads(response["body"])["status"] == "pending"


def test_timestamps_are_read_as_dynamodb_returns_them(result):
    """⚠️ DynamoDB has no float type - every number comes back as Decimal.

    Guarding on isinstance(x, (int, float)) reads them all as missing, which
    disables the abandonment check silently: a dead question reports `pending`
    forever. The other tests here pass plain floats and cannot see it.
    """
    from decimal import Decimal

    response = _call(
        result,
        {
            "status": "processing",
            "sessionId": SESSION_ID,
            "claimedAt": Decimal(int(time.time() - result.ABANDONED_AFTER_SECONDS - 1)),
        },
    )

    assert json.loads(response["body"])["status"] == "error"


def test_the_signed_url_points_at_the_regional_endpoint(monkeypatch):
    """⚠️ Caught by fetching a real URL, not by this suite.

    boto3 signing against the global endpoint (s3.amazonaws.com) makes S3
    answer with a 307 to the regional host rather than the audio. curl -L
    survives it; a media player handed the URL may not, and the rider gets an
    answer on screen with nothing to hear.

    The fixture elsewhere stubs generate_presigned_url, so only a test that
    lets the real signer run can see this.
    """
    monkeypatch.setenv("AUDIO_BUCKET", "bucket-test")
    monkeypatch.setenv("AWS_REGION", "ap-northeast-1")
    module = importlib.import_module("handlers.result")
    importlib.reload(module)

    url = module._audio_url("answers/req-1.mp3")

    # Regional host, virtual-hosted style: no redirect on the way to the object.
    assert "bucket-test.s3.ap-northeast-1.amazonaws.com" in url
    assert "s3.amazonaws.com/bucket-test" not in url
    # SigV4, not the legacy AWSAccessKeyId query form.
    assert "X-Amz-Signature=" in url
