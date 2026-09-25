"""Geo helpers: bearing between two GPS points, and its human-readable form.

The app sends two GPS points taken around the recording (see
docs/01_architecture.md). The direction of travel is derived here, on the
backend, rather than left to the LLM: bearing is plain trigonometry, and asking
the model to infer "which side is on the right" from raw coordinates is both
wasteful and unreliable.
"""

import math
from typing import TypedDict


class Coordinates(TypedDict):
    """A GPS point, matching the OpenAPI `Coordinates` schema."""

    latitude: float
    longitude: float


# 16-point compass, starting at north and going clockwise.
_COMPASS_POINTS: tuple[str, ...] = (
    "北",
    "北北東",
    "北東",
    "東北東",
    "東",
    "東南東",
    "南東",
    "南南東",
    "南",
    "南南西",
    "南西",
    "西南西",
    "西",
    "西北西",
    "北西",
    "北北西",
)

# Below this distance the two points are treated as the same place: GPS noise
# would dominate and produce a meaningless bearing (e.g. while stopped at a
# traffic light). See docs/01b_heading.md section 3.
MIN_DISTANCE_METERS = 5.0

_EARTH_RADIUS_METERS = 6_371_000.0


def haversine_distance(start: Coordinates, end: Coordinates) -> float:
    """Great-circle distance between two points, in meters."""
    lat1 = math.radians(start["latitude"])
    lat2 = math.radians(end["latitude"])
    delta_lat = lat2 - lat1
    delta_lon = math.radians(end["longitude"] - start["longitude"])

    a = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lon / 2) ** 2
    )
    return 2 * _EARTH_RADIUS_METERS * math.asin(math.sqrt(a))


def calculate_bearing(start: Coordinates, end: Coordinates) -> float:
    """Initial bearing from `start` to `end`, in degrees clockwise from north.

    Returns a value in [0, 360). This is the forward azimuth: the direction to
    head in at `start` to reach `end` along a great circle.
    """
    lat1 = math.radians(start["latitude"])
    lat2 = math.radians(end["latitude"])
    delta_lon = math.radians(end["longitude"] - start["longitude"])

    x = math.sin(delta_lon) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(
        delta_lon
    )
    return math.degrees(math.atan2(x, y)) % 360


def bearing_to_compass(bearing: float) -> str:
    """Convert a bearing in degrees to a 16-point Japanese compass label."""
    index = round(bearing / 22.5) % 16
    return _COMPASS_POINTS[index]


def relative_direction(bearing: float, offset_degrees: float) -> str:
    """Compass label for a direction `offset_degrees` clockwise of `bearing`.

    Used to describe what is to the rider's right (+90), left (-90), etc.
    """
    return bearing_to_compass((bearing + offset_degrees) % 360)
