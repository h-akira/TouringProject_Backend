"""Turn a GPS coordinate into a Japanese address.

Exists because the model gets this wrong on its own: asked to place a
coordinate, the deployed agent named a city about 40km away, and every answer
built on that was wrong with it. Coordinates are not something an LLM can
reliably invert, so the address is resolved here and handed over as fact
(pre-research/geocoding/).

Uses Amazon Location Service, which is callable from this Lambda's own region,
so no extra cross-region hop is added.
"""

import os
from typing import Any, Optional

import boto3

# Amazon Location is available in this stack's region (Tokyo), unlike the agent.
GEO_REGION = os.environ.get("GEO_REGION", "ap-northeast-1")

_client = None


def _get_client() -> Any:
    """Build the client lazily so importing this module needs no credentials."""
    global _client
    if _client is None:
        _client = boto3.client("geo-places", region_name=GEO_REGION)
    return _client


def describe_location(latitude: float, longitude: float) -> Optional[str]:
    """Return a human-readable place name, or None when it cannot be resolved.

    Returns prefecture + city (e.g. "神奈川県箱根町") rather than the full
    `Label`, which goes down to the building and would be both wrong to read
    aloud and more precise than anything here needs to store.

    None means "no answer": open sea and anywhere outside coverage land there,
    and the caller is expected to carry on without an address rather than fail.
    """
    try:
        # Longitude first - the reverse order silently resolves elsewhere.
        result = _get_client().reverse_geocode(
            QueryPosition=[longitude, latitude],
            MaxResults=1,
        )
    except Exception as error:  # noqa: BLE001 - never fail the question over this
        print(f"reverse_geocode failed: {type(error).__name__}: {error}")
        return None

    items = result.get("ResultItems") or []
    if not items:
        return None

    address = items[0].get("Address") or {}
    region = (address.get("Region") or {}).get("Name") or ""
    # Locality is the city/town/village; SubRegion is the county, which matters
    # in rural areas where Locality can be missing.
    locality = address.get("Locality") or address.get("SubRegion") or ""

    place = f"{region}{locality}".strip()
    return place or None
