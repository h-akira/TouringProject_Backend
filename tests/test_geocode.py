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


def _item(region=None, locality=None, sub_region=None, label=None,
          district=None, sub_district=None):
    address = {}
    if region is not None:
        address["Region"] = {"Name": region}
    if locality is not None:
        address["Locality"] = locality
    if sub_region is not None:
        # The API returns SubRegion as a structure, like Region.
        address["SubRegion"] = {"Name": sub_region}
    if district is not None:
        address["District"] = district
    if sub_district is not None:
        address["SubDistrict"] = sub_district
    if label is not None:
        address["Label"] = label
    return {"ResultItems": [{"Address": address}]}


def test_prefecture_and_city_are_joined(geocode):
    _stub(geocode, _item(region="神奈川県", locality="箱根町"))
    assert geocode.describe_location(35.2323, 139.0230) == "神奈川県箱根町"


def test_district_is_appended_to_the_city(geocode):
    _stub(geocode, _item(region="神奈川県", locality="箱根町", sub_district="湯本"))
    assert geocode.describe_location(35.2329, 139.1056) == "神奈川県箱根町湯本"


def test_ward_of_a_designated_city_is_included(geocode):
    _stub(geocode, _item(region="北海道", locality="札幌市", district="北区",
                         sub_district="北6条西"))
    assert geocode.describe_location(43.0687, 141.3508) == "北海道札幌市北区北6条西"


def test_district_already_in_the_name_is_not_repeated(geocode):
    _stub(geocode, _item(region="東京都", locality="千代田区", district="千代田区",
                         sub_district="丸の内"))
    assert geocode.describe_location(35.6812, 139.7671) == "東京都千代田区丸の内"


def test_district_without_a_city_is_dropped(geocode):
    # A town name with no city around it would be ambiguous.
    _stub(geocode, _item(region="静岡県", sub_district="どこかの町"))
    assert geocode.describe_location(34.7, 138.9) == "静岡県"


def test_japanese_names_are_requested(geocode):
    capture: dict = {}
    _stub(geocode, _item(region="東京都", locality="千代田区"), capture=capture)
    geocode.describe_location(35.6812, 139.7671)

    assert capture["Language"] == "ja"


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
