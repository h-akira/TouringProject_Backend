"""Tests for the Polly wrapper.

The behaviour worth pinning down is what happens when synthesis fails: the
answer already exists by then, and a rider who gets text without audio has lost
the convenience, not the answer. So none of these failures may raise.
"""

import importlib

import pytest


class _FakeStream:
    def __init__(self, payload=b"mp3-bytes"):
        self._payload = payload

    def read(self):
        return self._payload


@pytest.fixture
def speech(monkeypatch):
    monkeypatch.setenv("AUDIO_BUCKET", "bucket-test")
    module = importlib.import_module("lib.speech")
    importlib.reload(module)
    return module


def _stub(speech, capture=None, fail_on=None):
    def fake_synthesize_speech(**kwargs):
        if fail_on == "polly":
            raise RuntimeError("polly unavailable")
        if capture is not None:
            capture["polly"] = kwargs
        return {"AudioStream": _FakeStream()}

    def fake_put_object(**kwargs):
        if fail_on == "s3":
            raise RuntimeError("bucket unavailable")
        if capture is not None:
            capture["put"] = kwargs

    speech._polly.synthesize_speech = fake_synthesize_speech
    speech._s3.put_object = fake_put_object


def test_audio_is_stored_under_the_request_id(speech):
    capture: dict = {}
    _stub(speech, capture=capture)

    key = speech.synthesize("req-1", "それは富士山です")

    assert key == "answers/req-1.mp3"
    assert capture["put"]["Key"] == key
    assert capture["put"]["Body"] == b"mp3-bytes"


def test_japanese_voice_is_requested(speech):
    # An English voice reading Japanese is unusable, and the default is English.
    capture: dict = {}
    _stub(speech, capture=capture)

    speech.synthesize("req-1", "それは富士山です")

    assert capture["polly"]["LanguageCode"] == "ja-JP"
    assert capture["polly"]["Text"] == "それは富士山です"
    # ⚠️ Neural, and a voice that supports it. Japanese has four voices but
    # Mizuki is standard-only, so pairing her with this engine would fail the
    # call outright - and the answer would arrive with no audio at all.
    assert capture["polly"]["Engine"] == "neural"
    assert capture["polly"]["VoiceId"] == speech.VOICE_ID
    assert speech.VOICE_ID in ("Kazuha", "Tomoko", "Takumi")


def test_polly_failure_does_not_take_the_answer_with_it(speech):
    # The answer is already generated and worth delivering; losing the audio
    # must not fail the question.
    _stub(speech, fail_on="polly")

    assert speech.synthesize("req-1", "それは富士山です") is None


def test_storage_failure_does_not_take_the_answer_with_it(speech):
    _stub(speech, fail_on="s3")

    assert speech.synthesize("req-1", "それは富士山です") is None


def test_empty_answer_is_not_synthesised(speech):
    capture: dict = {}
    _stub(speech, capture=capture)

    assert speech.synthesize("req-1", "") is None
    assert "polly" not in capture


def test_missing_bucket_is_survivable(speech, monkeypatch):
    monkeypatch.setenv("AUDIO_BUCKET", "")
    module = importlib.reload(speech)
    _stub(module)

    assert module.synthesize("req-1", "それは富士山です") is None
