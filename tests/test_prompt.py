"""Tests for the current-time line in the agent's prompt.

Without it the model assumed the date its training data suggested and read
freshly searched articles as coming from the future (seen on a ride test).

Coordinates here are public landmarks, never real test locations (the parent AGENTS.md, public-repository rules).
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lib import prompt  # noqa: E402

# Tokyo Station.
TOKYO_STATION = {"latitude": 35.6812, "longitude": 139.7671}


@pytest.fixture(autouse=True)
def _no_geocoding(monkeypatch):
    # The address lookup calls Amazon Location; it is not what is under test.
    monkeypatch.setattr(prompt, "describe_location", lambda _lat, _lon: None)


def test_now_is_stated_in_japan_time():
    # 2026-09-25 23:30 UTC is already Saturday the 26th in Japan.
    now = datetime(2026, 9, 25, 23, 30, tzinfo=timezone.utc)
    assert prompt.format_now(now) == "【現在日時: 2026年9月26日（土）8時30分（日本時間）】"


def test_minutes_are_zero_padded():
    now = datetime(2026, 9, 26, 14, 5, tzinfo=prompt.JST)
    assert "14時05分" in prompt.format_now(now)


def test_now_defaults_to_the_current_time():
    # No argument means "now"; only the shape is checked, not the value.
    assert prompt.format_now().startswith("【現在日時: ")


def test_prompt_starts_with_the_current_time():
    now = datetime(2026, 9, 26, 14, 5, tzinfo=prompt.JST)
    text = prompt.build("今日の天気は？", TOKYO_STATION, None, now=now)
    first_line = text.splitlines()[0]
    assert first_line == "【現在日時: 2026年9月26日（土）14時05分（日本時間）】"
    assert "現在地: 緯度 35.6812, 経度 139.7671" in text
    assert text.endswith("質問: 今日の天気は？")


@pytest.mark.parametrize("start", [None, {"latitude": "x", "longitude": 139.7}])
def test_time_is_stated_even_without_a_location(start):
    # The time does not depend on the position, so it goes in regardless.
    now = datetime(2026, 9, 26, 14, 5, tzinfo=prompt.JST)
    text = prompt.build("今日は何曜日？", start, None, now=now)
    assert text == "【現在日時: 2026年9月26日（土）14時05分（日本時間）】\n\n質問: 今日は何曜日？"
