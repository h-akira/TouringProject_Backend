"""Tests for reassembling the agent's answer from its SSE stream.

What the rider hears is exactly what comes out of here, so the thing that
matters is which text makes it in - not just that the deltas are joined.
"""

import json

import pytest

from lib.agent import _extract_answer


class _FakeStream:
    def __init__(self, events: list):
        self._lines = [f"data: {json.dumps(event)}".encode() for event in events]

    def iter_lines(self):
        return iter(self._lines)


def _text(text: str) -> dict:
    return {"event": {"contentBlockDelta": {"delta": {"text": text}}}}


def _tool_use_start() -> dict:
    return {
        "event": {
            "contentBlockStart": {
                "start": {"toolUse": {"toolUseId": "t-1", "name": "webSearch___WebSearch"}}
            }
        }
    }


def test_text_deltas_are_joined():
    stream = _FakeStream([_text("今日は"), _text("晴れです。")])

    assert _extract_answer(stream) == "今日は晴れです。"


def test_text_before_a_search_is_not_read_aloud():
    # Seen on the road: "I can't be sure, let me look it up" followed by the
    # actual answer, all read out as one.
    stream = _FakeStream(
        [
            _text("確実にはお答えできません。今すぐ調べます。"),
            _tool_use_start(),
            _text("現在の首相は〇〇さんです。"),
        ]
    )

    assert _extract_answer(stream) == "現在の首相は〇〇さんです。"


def test_only_text_after_the_last_search_is_kept():
    stream = _FakeStream(
        [
            _text("調べます。"),
            _tool_use_start(),
            _text("もう一度調べます。"),
            _tool_use_start(),
            _text("答えです。"),
        ]
    )

    assert _extract_answer(stream) == "答えです。"


def test_answer_without_a_search_is_kept_whole():
    stream = _FakeStream([_text("山の名前の由来は"), _text("〜です。")])

    assert _extract_answer(stream) == "山の名前の由来は〜です。"


def test_agent_error_is_raised():
    stream = _FakeStream([{"error": "`question` (or `prompt`) is required"}])

    with pytest.raises(RuntimeError):
        _extract_answer(stream)
