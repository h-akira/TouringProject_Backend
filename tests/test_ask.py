"""Tests for the POST /ask handler.

The handler no longer waits for an answer - it validates the request, settles
the address and heading into a prompt, and queues it (docs/01a). So
these cover that job: validation, session handling, and what ends up in the
stored prompt. The agent itself is exercised in test_worker.py.

DynamoDB and SQS are stubbed rather than mocked at the boto3 layer: what
matters here is the prompt text handed onward, not the wire format.
"""

import importlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


@pytest.fixture
def ask(monkeypatch):
    """Import the handler with a queue configured, isolated per test.

    Reverse-geocoding is stubbed out by default so these tests neither need
    credentials nor depend on Amazon Location's answers; the tests that care
    about the address override it.

    ⚠️ The stub goes on lib.prompt, which is what actually calls it - patching
    the handler would leave the real Amazon Location call in place and these
    tests would quietly start needing credentials.
    """
    monkeypatch.setenv("QUEUE_URL", "https://sqs.example/queue")
    prompt_module = importlib.import_module("lib.prompt")
    importlib.reload(prompt_module)
    prompt_module.describe_location = lambda _lat, _lon: None
    module = importlib.import_module("handlers.ask")
    importlib.reload(module)
    # The address stub lives on lib.prompt; a test that wants a specific
    # address sets it there, via this alias.
    module.prompt_builder = prompt_module
    return module


def _event(body) -> dict:
    return {"body": body if isinstance(body, str) else json.dumps(body)}


def _call(ask, body, capture=None, fail_on=None):
    """Invoke the handler with the table and queue stubbed out.

    `capture` collects what would have been persisted/sent, so a test can
    assert on the prompt without a real table.
    """
    def fake_create_pending(request_id, session_id, prompt):
        if fail_on == "store":
            raise RuntimeError("table unavailable")
        if capture is not None:
            capture.update(
                {"requestId": request_id, "sessionId": session_id, "prompt": prompt}
            )

    def fake_send_message(**kwargs):
        if fail_on == "queue":
            raise RuntimeError("queue unavailable")
        if capture is not None:
            capture["message"] = kwargs

    ask.store.create_pending = fake_create_pending
    ask._sqs.send_message = fake_send_message
    return ask.handler(_event(body), None)


def _prompt(capture: dict) -> str:
    return capture["prompt"]


def test_question_is_accepted_not_answered(ask):
    capture: dict = {}
    result = _call(ask, {"question": "富士山とは？"}, capture=capture)
    body = json.loads(result["body"])

    # 202, because the answer does not exist yet - the app polls for it.
    assert result["statusCode"] == 202
    assert body["requestId"]
    assert "answer" not in body
    # A session id is generated so the app can continue the conversation.
    assert len(body["sessionId"]) >= ask.MIN_SESSION_ID_CHARS


def test_the_queued_message_names_the_stored_request(ask):
    # The worker is handed only an id; it reads the prompt back from the table.
    capture: dict = {}
    result = _call(ask, {"question": "q"}, capture=capture)

    queued = json.loads(capture["message"]["MessageBody"])
    assert queued["requestId"] == capture["requestId"]
    assert queued["requestId"] == json.loads(result["body"])["requestId"]


def test_supplied_session_id_is_passed_through(ask):
    session_id = "touring-" + "a" * 32
    capture: dict = {}
    result = _call(ask, {"question": "続き", "sessionId": session_id}, capture=capture)

    # The same id must be stored, or the conversation would restart.
    assert capture["sessionId"] == session_id
    assert json.loads(result["body"])["sessionId"] == session_id


def test_generated_session_ids_differ_between_conversations(ask):
    first = json.loads(_call(ask, {"question": "q"})["body"])["sessionId"]
    second = json.loads(_call(ask, {"question": "q"})["body"])["sessionId"]

    assert first != second


def test_location_is_prepended_to_the_prompt(ask):
    capture: dict = {}
    _call(
        ask,
        {"question": "この山は？", "start": {"latitude": 35.5, "longitude": 139.5}},
        capture=capture,
    )

    prompt = _prompt(capture)
    assert "35.5" in prompt and "139.5" in prompt
    assert "この山は？" in prompt


def test_resolved_address_is_stated_as_fact(ask):
    # The model places coordinates unreliably, so the address is given to it
    # along with an instruction not to second-guess the numbers.
    ask.prompt_builder.describe_location = lambda _lat, _lon: "神奈川県箱根町"
    capture: dict = {}
    _call(
        ask,
        {"question": "この辺の名物は？", "start": {"latitude": 35.23, "longitude": 139.02}},
        capture=capture,
    )

    prompt = _prompt(capture)
    assert "神奈川県箱根町" in prompt
    assert "推測しないこと" in prompt


def test_question_still_sent_when_the_address_is_unknown(ask):
    # At sea the lookup returns nothing; the question must go through anyway.
    ask.prompt_builder.describe_location = lambda _lat, _lon: None
    capture: dict = {}
    _call(
        ask,
        {"question": "ここはどこ？", "start": {"latitude": 35.0, "longitude": 141.5}},
        capture=capture,
    )

    prompt = _prompt(capture)
    assert "ここはどこ？" in prompt
    assert "現在地の住所" not in prompt


def test_heading_is_resolved_before_the_prompt_is_built(ask):
    # US-2.03. The bearing is stated outright, along with which way is right,
    # because the agent is told not to work directions out for itself.
    # `start` is where the rider is now and `end` the older fix, so travelling
    # north means `end` sits to the SOUTH of `start`.
    capture: dict = {}
    _call(
        ask,
        {
            "question": "右手に見える山は？",
            "start": {"latitude": 36.0, "longitude": 139.0},
            "end": {"latitude": 35.0, "longitude": 139.0},
        },
        capture=capture,
    )

    prompt = _prompt(capture)
    assert "進行方向: 北" in prompt
    assert "右手は東" in prompt
    assert "左手は西" in prompt


def test_heading_is_not_reversed(ask):
    # Guards the end -> start reading: getting it backwards would still produce
    # a plausible-looking heading, just the opposite one, and would tell the
    # rider that the mountain on their right is on their left.
    capture: dict = {}
    _call(
        ask,
        {
            "question": "右手に見える山は？",
            "start": {"latitude": 35.0, "longitude": 139.0},
            "end": {"latitude": 36.0, "longitude": 139.0},  # came from the north
        },
        capture=capture,
    )

    prompt = _prompt(capture)
    assert "進行方向: 南" in prompt
    assert "右手は西" in prompt


def test_no_heading_when_the_rider_has_not_moved(ask):
    # Stopped at a light: the two points are metres apart and the bearing would
    # be GPS noise. Better to say nothing than to point the rider the wrong way.
    capture: dict = {}
    same = {"latitude": 35.0, "longitude": 139.0}
    _call(ask, {"question": "この辺は？", "start": same, "end": same}, capture=capture)

    prompt = _prompt(capture)
    assert "進行方向" not in prompt
    assert "この辺は？" in prompt


def test_no_heading_when_the_two_points_are_close_but_not_identical(ask):
    # The realistic stationary case: GPS drift means a stopped rider's two
    # fixes are never exactly equal, just a metre or two apart. Comparing for
    # equality would miss this and hand the model a heading built from noise.
    capture: dict = {}
    _call(
        ask,
        {
            "question": "この辺は？",
            "start": {"latitude": 35.0, "longitude": 139.0},
            # ~1.1m north - well inside MIN_DISTANCE_METERS.
            "end": {"latitude": 35.00001, "longitude": 139.0},
        },
        capture=capture,
    )

    prompt = _prompt(capture)
    assert "進行方向" not in prompt
    assert "この辺は？" in prompt


def test_heading_appears_once_the_rider_clears_the_threshold(ask):
    # The other side of the same boundary, so the threshold cannot be raised
    # far enough to suppress headings for a genuinely moving rider.
    capture: dict = {}
    _call(
        ask,
        {
            "question": "右手の山は？",
            "start": {"latitude": 35.001, "longitude": 139.0},  # ~111m north
            "end": {"latitude": 35.0, "longitude": 139.0},
        },
        capture=capture,
    )

    assert "進行方向: 北" in _prompt(capture)


def test_elapsed_time_is_stated_for_a_later_question(ask):
    # Each turn carries its own position, so the agent needs to know how much
    # time (and therefore distance) separates them to resolve "that mountain".
    capture: dict = {}
    _call(
        ask,
        {
            "question": "さっきの山の標高は？",
            "start": {"latitude": 35.0, "longitude": 139.0},
            "elapsedSeconds": 185,
        },
        capture=capture,
    )

    assert "約3分後" in _prompt(capture)


def test_no_elapsed_note_on_the_first_question(ask):
    capture: dict = {}
    _call(
        ask,
        {"question": "この辺は？", "start": {"latitude": 35.0, "longitude": 139.0}},
        capture=capture,
    )

    assert "分後" not in _prompt(capture)


def test_no_elapsed_note_when_barely_any_time_has_passed(ask):
    # Asking a follow-up straight away: the rider has not moved, so the note
    # would be noise.
    capture: dict = {}
    _call(
        ask,
        {
            "question": "もっと詳しく",
            "start": {"latitude": 35.0, "longitude": 139.0},
            "elapsedSeconds": 20,
        },
        capture=capture,
    )

    assert "分後" not in _prompt(capture)


@pytest.mark.parametrize("value", ["180", -5, True, None, 1.5])
def test_bogus_elapsed_seconds_is_ignored(ask, value):
    # A malformed value must not take the answer down with it.
    capture: dict = {}
    result = _call(
        ask,
        {
            "question": "この辺は？",
            "start": {"latitude": 35.0, "longitude": 139.0},
            "elapsedSeconds": value,
        },
        capture=capture,
    )

    assert result["statusCode"] == 202
    assert "分後" not in _prompt(capture)


def test_no_heading_when_the_second_point_is_absent(ask):
    # `end` is optional in the contract; the question still goes through.
    capture: dict = {}
    _call(
        ask,
        {"question": "ここはどこ？", "start": {"latitude": 35.0, "longitude": 139.0}},
        capture=capture,
    )

    prompt = _prompt(capture)
    assert "進行方向" not in prompt
    assert "ここはどこ？" in prompt


def test_malformed_second_point_is_ignored(ask):
    # A partial `end` must not take the answer down with it.
    capture: dict = {}
    _call(
        ask,
        {
            "question": "この辺は？",
            "start": {"latitude": 35.0, "longitude": 139.0},
            "end": {"latitude": 36.0},
        },
        capture=capture,
    )

    prompt = _prompt(capture)
    assert "進行方向" not in prompt
    assert "この辺は？" in prompt


@pytest.mark.parametrize(
    "body",
    [
        {},                                    # no question
        {"question": "   "},                   # blank question
        {"question": "x" * 501},               # too long
        {"question": "q", "sessionId": "short"},  # session id under 33 chars
    ],
)
def test_invalid_requests_are_rejected(ask, body):
    result = _call(ask, body)
    assert result["statusCode"] == 400
    assert "error" in json.loads(result["body"])


def test_malformed_json_is_rejected(ask):
    result = _call(ask, "{not json")
    assert result["statusCode"] == 400


@pytest.mark.parametrize("fail_on", ["store", "queue"])
def test_failure_to_accept_does_not_leak_details(ask, fail_on):
    # A question that cannot be queued is never going to be answered, so the
    # app is told now rather than left polling for something that will not come.
    result = _call(ask, {"question": "q"}, fail_on=fail_on)

    assert result["statusCode"] == 502
    # The AWS error may name internal resources, so it must not be echoed.
    assert "unavailable" not in result["body"]


def test_missing_queue_url_is_reported(monkeypatch):
    monkeypatch.delenv("QUEUE_URL", raising=False)
    module = importlib.import_module("handlers.ask")
    importlib.reload(module)

    result = module.handler(_event({"question": "q"}), None)
    assert result["statusCode"] == 502
