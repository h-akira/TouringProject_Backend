"""Tests for reverse-geocoding.

The AWS call is stubbed: these cover how the response is interpreted, and in
particular that an unresolvable position degrades to None instead of raising -
a rider at sea should still get an answer.
"""

import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


@pytest.fixture
def geocode():
    module = importlib.import_module("handlers.geocode")
    importlib.reload(module)
    return module


def _stub(geocode, result=None, error=None, capture=None):
    class _Client:
        def reverse_geocode(self, **kwargs):
            if capture is not None:
                capture.update(kwargs)
            if error is not None:
                raise error
            return result if result is not None else {"ResultItems": []}

    geocode._client = _Client()


def _item(region=None, locality=None, sub_region=None, label=None):
    address = {}
    if region is not None:
        address["Region"] = {"Name": region}
    if locality is not None:
        address["Locality"] = locality
    if sub_region is not None:
        address["SubRegion"] = sub_region
    if label is not None:
        address["Label"] = label
    return {"ResultItems": [{"Address": address}]}


def test_prefecture_and_city_are_joined(geocode):
    _stub(geocode, _item(region="神奈川県", locality="箱根町"))
    assert geocode.describe_location(35.2323, 139.0230) == "神奈川県箱根町"


def test_longitude_is_sent_first(geocode):
    # Swapping these silently resolves to the wrong place, so it is pinned.
    capture: dict = {}
    _stub(geocode, _item(region="東京都", locality="千代田区"), capture=capture)
    geocode.describe_location(35.6812, 139.7671)

    assert capture["QueryPosition"] == [139.7671, 35.6812]


def test_sub_region_is_used_when_locality_is_missing(geocode):
    # Rural coordinates can come back without a Locality.
    _stub(geocode, _item(region="静岡県", sub_region="賀茂郡"))
    assert geocode.describe_location(34.7, 138.9) == "静岡県賀茂郡"


def test_label_is_not_used(geocode):
    # Label goes down to the building; it must not leak into the prompt.
    _stub(geocode, _item(label="〒100-0005 東京都千代田区丸の内1丁目9 どこかの店"))
    assert geocode.describe_location(35.6812, 139.7671) is None


def test_no_results_returns_none(geocode):
    # Open sea: the caller carries on without an address.
    _stub(geocode, {"ResultItems": []})
    assert geocode.describe_location(35.0, 141.5) is None


def test_aws_error_returns_none_instead_of_raising(geocode):
    # A geocoding outage must not cost the rider their answer.
    _stub(geocode, error=RuntimeError("AccessDeniedException"))
    assert geocode.describe_location(35.0, 139.0) is None
