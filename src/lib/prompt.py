"""Turns a question and the rider's position into the prompt for the agent.

This is the "settle the facts before the model sees them" step
(docs/01_architecture.md section 4): the address is resolved and the heading is
computed here, so the model is told where the rider is rather than asked to
work it out from coordinates - which it does badly (learning/61 section 2).

Both request paths need it. A typed question goes through handlers/ask.py,
which builds the prompt while the app is still on the request; a recorded one
cannot, because transcription finishes long after that response has gone, so
handlers/transcribe_done.py builds it when the transcript arrives. Same
question, same facts, one implementation.
"""

from typing import Any, Optional

from handlers.geocode import describe_location
from lib.geo import (
    MIN_DISTANCE_METERS,
    bearing_to_compass,
    calculate_bearing,
    haversine_distance,
    relative_direction,
)


def format_elapsed(elapsed_seconds: Any) -> Optional[str]:
    """Say how long since the conversation started, for the agent's benefit.

    Every turn carries its own position, so the history ends up holding several
    of them. Without a sense of time the agent cannot tell whether "that
    mountain" refers to where the rider is now or where they were asked three
    minutes and two kilometres ago. Elapsed time is used rather than a
    timestamp: it is what the agent actually needs, and it keeps a log of the
    rider's movements out of the prompt.
    """
    if not isinstance(elapsed_seconds, int) or isinstance(elapsed_seconds, bool):
        return None
    if elapsed_seconds < 0:
        return None
    # Below a minute the rider has not meaningfully moved, so the note would be
    # noise; the first question of a conversation has no elapsed time at all.
    if elapsed_seconds < 60:
        return None

    minutes = elapsed_seconds // 60
    return f"【この質問は、会話の最初の質問から約{minutes}分後のものです】"


def describe_heading(lat: float, lon: float, end: Optional[dict]) -> Optional[str]:
    """Describe the direction of travel, and which way is left and right.

    The bearing is computed here rather than described to the model as two
    coordinate pairs: it is plain trigonometry, and the model places coordinates
    unreliably (learning/61 section 2).

    Note the direction the two points are read in. `start` is the rider's
    current position (it is what the address is resolved from), so `end` is the
    OLDER fix and the rider is travelling away from it - the bearing therefore
    runs end -> start. Reading them the other way round points the heading
    backwards, which swaps the rider's left and right.

    Returns None whenever a heading would be meaningless - no second point, or
    the rider has barely moved - so a stationary rider simply gets no heading
    rather than one derived from GPS jitter.
    """
    if not isinstance(end, dict):
        return None

    end_lat, end_lon = end.get("latitude"), end.get("longitude")
    if not isinstance(end_lat, (int, float)) or not isinstance(end_lon, (int, float)):
        return None

    current = {"latitude": lat, "longitude": lon}
    previous = {"latitude": end_lat, "longitude": end_lon}
    if haversine_distance(previous, current) < MIN_DISTANCE_METERS:
        return None

    bearing = calculate_bearing(previous, current)
    # Spelled out for the model, which is told not to work directions out for
    # itself (see the agent's system prompt).
    return (
        f"進行方向: {bearing_to_compass(bearing)}（真北から{round(bearing)}度）\n"
        f"ライダーから見て右手は{relative_direction(bearing, 90)}、"
        f"左手は{relative_direction(bearing, -90)}の方角"
    )


def build(
    question: str,
    start: Optional[dict],
    end: Optional[dict],
    elapsed_seconds: Any = None,
) -> str:
    """Prepend the rider's location, which the agent's prompt expects.

    The address is resolved here rather than left to the model, which places
    coordinates unreliably (see handlers/geocode.py). The raw coordinates go in
    too, since they are what any later tool call would need.
    """
    if not isinstance(start, dict):
        return question

    lat, lon = start.get("latitude"), start.get("longitude")
    if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
        return question

    context = ""
    elapsed = format_elapsed(elapsed_seconds)
    if elapsed:
        context += f"{elapsed}\n"
    context += f"現在地: 緯度 {lat}, 経度 {lon}"

    # Stated as fact so the model uses it instead of guessing from the numbers.
    place = describe_location(lat, lon)
    if place:
        context += f"\n現在地の住所: {place}（この住所は正確です。自分で座標から推測しないこと）"
    heading = describe_heading(lat, lon, end)
    if heading:
        context += f"\n{heading}"

    return f"{context}\n\n質問: {question}"
