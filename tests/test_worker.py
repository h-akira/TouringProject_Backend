"""Tests for the SQS worker.

The point of this handler is that it survives being run twice for the same
question - SQS delivers at least once, and a repeat would call the agent, and
be billed, again. Most of what follows is about that guarantee.

Both the table and the agent are stubbed; the agent's own stream parsing is
covered where that code lives.
"""

import importlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


class _FakeStore:
    """Stands in for the DynamoDB record, with a claim that can only win once."""

    def __init__(self, record=None, claimable=True):
        self.record = record if record is not None else {
            "prompt": "現在地: …\n\n質問: この山は？",
            "sessionId": "touring-" + "a" * 32,
        }
        self.claimable = claimable
        self.claims = 0
        self.saved_answer = None
        self.saved_error = None

    def claim(self, _request_id):
        self.claims += 1
        if not self.claimable:
            return None
        # A real claim is a conditional write: only the first one succeeds.
        self.claimable = False
        return self.record

    def save_answer(self, _request_id, answer, audio_key=None):
        self.saved_answer = answer
        self.saved_audio_key = audio_key

    def save_error(self, _request_id, message):
        self.saved_error = message


@pytest.fixture
def worker():
    """Import the worker with speech synthesis stubbed out.

    Polly is stubbed by default because these tests are about the agent and the
    stored answer; the tests that care about audio set their own stub. ⚠️ Note
    it goes on the module the worker imported, not on lib.speech's internals -
    patching further in would leave the real client in place.
    """
    module = importlib.import_module("handlers.worker")
    importlib.reload(module)
    module.speech.synthesize = lambda _request_id, _text: None
    return module


def _event(*request_ids: str) -> dict:
    return {
        "Records": [
            {"body": json.dumps({"requestId": rid})} for rid in request_ids
        ]
    }


def _run(worker, store, answer="それは富士山です", error=None):
    calls = []

    def fake_ask(prompt, session_id):
        calls.append((prompt, session_id))
        if error is not None:
            raise error
        return answer

    worker.store = store
    worker.agent.ask = fake_ask
    return calls


def test_answer_is_stored(worker):
    store = _FakeStore()
    calls = _run(worker, store)

    worker.handler(_event("req-1"), None)

    assert store.saved_answer == "それは富士山です"
    assert len(calls) == 1
    # The prompt built by /ask is what reaches the agent, unchanged.
    assert calls[0][0] == store.record["prompt"]
    assert calls[0][1] == store.record["sessionId"]


def test_duplicate_delivery_does_not_call_the_agent_twice(worker):
    # The whole reason `processing` exists: SQS can deliver the same message
    # again, and calling the agent twice would bill twice.
    store = _FakeStore()
    calls = _run(worker, store)

    worker.handler(_event("req-1"), None)
    worker.handler(_event("req-1"), None)

    assert store.claims == 2
    assert len(calls) == 1


def test_unclaimable_record_is_skipped(worker):
    store = _FakeStore(claimable=False)
    calls = _run(worker, store)

    worker.handler(_event("req-1"), None)

    assert calls == []
    assert store.saved_answer is None
    assert store.saved_error is None


def test_agent_failure_is_recorded_without_leaking_details(worker):
    store = _FakeStore()
    _run(
        worker,
        store,
        error=RuntimeError("arn:aws:iam::123456789012:role/secret not authorized"),
    )

    worker.handler(_event("req-1"), None)

    assert store.saved_answer is None
    # The stored message is shown to the rider, so it must not name internals.
    assert "123456789012" not in (store.saved_error or "")


def test_agent_failure_does_not_raise(worker):
    # Raising would return the message to the queue, and the retry would call
    # the agent again - the failure is already recorded on the item.
    store = _FakeStore()
    _run(worker, store, error=RuntimeError("boom"))

    worker.handler(_event("req-1"), None)  # must not raise

    assert store.saved_error is not None


def test_empty_answer_is_recorded_as_an_error(worker):
    store = _FakeStore()
    _run(worker, store, answer="")

    worker.handler(_event("req-1"), None)

    assert store.saved_answer is None
    assert store.saved_error is not None


def test_incomplete_record_is_recorded_as_an_error(worker):
    store = _FakeStore(record={"sessionId": "touring-" + "a" * 32})  # no prompt
    calls = _run(worker, store)

    worker.handler(_event("req-1"), None)

    assert calls == []
    assert store.saved_error is not None


@pytest.mark.parametrize(
    "body",
    ["{not json", json.dumps({}), json.dumps({"requestId": ""})],
)
def test_malformed_messages_are_skipped(worker, body):
    store = _FakeStore()
    calls = _run(worker, store)

    worker.handler({"Records": [{"body": body}]}, None)  # must not raise

    assert calls == []
    assert store.claims == 0
