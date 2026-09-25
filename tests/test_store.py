"""Tests for the DynamoDB access layer.

Mostly about two things the design depends on: the key layout that lets one
table hold every entity type (docs/03_dynamodb_table.md), and the conditional
write that makes duplicate SQS deliveries harmless.
"""

import importlib
import sys
import time
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

SESSION_ID = "touring-" + "a" * 32


class _FakeTable:
    def __init__(self, update_error=None):
        self.item = None
        self.updates = []
        self.update_error = update_error

    def put_item(self, Item):  # noqa: N803 - boto3's own casing
        self.item = Item

    def update_item(self, **kwargs):
        if self.update_error is not None:
            raise self.update_error
        self.updates.append(kwargs)
        return {"Attributes": self.item or {}}

    def get_item(self, Key):  # noqa: N803 - boto3's own casing
        return {"Item": self.item} if self.item else {}


@pytest.fixture
def store(monkeypatch):
    monkeypatch.setenv("TABLE_NAME", "dynamodb-trg-test-main")
    module = importlib.import_module("lib.store")
    importlib.reload(module)
    return module


def _use(store, table):
    store._table = lambda: table
    return table


def _conditional_failure():
    return ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException", "Message": "no"}},
        "UpdateItem",
    )


def test_pending_record_uses_the_single_table_key_layout(store):
    table = _use(store, _FakeTable())
    store.create_pending("req-1", SESSION_ID, "現在地: …")

    # "ASK#" prefixed pk is what keeps other entity types from colliding.
    assert table.item["pk"] == "ASK#req-1"
    assert table.item["sk"] == "STATUS"
    assert table.item["status"] == "pending"
    assert table.item["sessionId"] == SESSION_ID


def test_pending_record_expires(store):
    table = _use(store, _FakeTable())
    before = int(time.time())
    store.create_pending("req-1", SESSION_ID, "現在地: …")

    # Answers can name where the rider has been, so records do not linger.
    assert table.item["expiresAt"] >= before + store.TTL_SECONDS
    assert table.item["expiresAt"] <= int(time.time()) + store.TTL_SECONDS


def test_claim_returns_the_record_when_it_wins(store):
    table = _use(store, _FakeTable())
    table.item = {"status": "pending", "sessionId": SESSION_ID}

    assert store.claim("req-1") is not None
    # Only a record still in `pending` may be claimed - that is the guard.
    condition = table.updates[0]["ConditionExpression"]
    assert "pending" in str(table.updates[0]["ExpressionAttributeValues"])
    assert condition


def test_claim_returns_none_when_someone_else_has_it(store):
    # A duplicate SQS delivery lands here; returning None is what stops the
    # agent being called (and billed) a second time.
    _use(store, _FakeTable(update_error=_conditional_failure()))

    assert store.claim("req-1") is None


def test_claim_records_when_it_was_taken(store):
    # Without this timestamp a worker killed mid-flight - a Lambda timeout
    # records nothing - would strand the record in `processing` forever.
    table = _use(store, _FakeTable())
    before = int(time.time())
    store.claim("req-1")

    values = table.updates[0]["ExpressionAttributeValues"]
    assert values[":now"] >= before
    assert "claimedAt" in table.updates[0]["UpdateExpression"]


def test_a_stale_claim_can_be_taken_over(store):
    # The condition has to admit an abandoned record, or nothing ever retries
    # a question whose worker died.
    table = _use(store, _FakeTable())
    store.claim("req-1")

    condition = table.updates[0]["ConditionExpression"]
    values = table.updates[0]["ExpressionAttributeValues"]
    assert ":pending" in condition and ":stale" in condition
    # The cutoff must sit far enough back that a worker still running cannot
    # have its question taken - otherwise the agent gets called twice.
    assert values[":now"] - values[":stale"] == store.CLAIM_STALE_SECONDS
    assert store.CLAIM_STALE_SECONDS > 180  # the queue's visibility timeout


def test_claim_propagates_unexpected_errors(store):
    # A throttle or a missing table must not be mistaken for "already claimed",
    # which would silently drop the question.
    error = ClientError(
        {"Error": {"Code": "ProvisionedThroughputExceededException", "Message": "slow"}},
        "UpdateItem",
    )
    _use(store, _FakeTable(update_error=error))

    with pytest.raises(ClientError):
        store.claim("req-1")


def test_saving_an_answer_drops_the_prompt(store):
    # The prompt carries the resolved address; once answered it has no further
    # use, so it does not sit in the table until the TTL fires.
    table = _use(store, _FakeTable())
    store.save_answer("req-1", "それは富士山です")

    assert "REMOVE prompt" in table.updates[0]["UpdateExpression"]


def test_saving_an_error_drops_the_prompt(store):
    table = _use(store, _FakeTable())
    store.save_error("req-1", "The agent could not be reached.")

    assert "REMOVE prompt" in table.updates[0]["UpdateExpression"]


def test_get_returns_none_when_absent(store):
    _use(store, _FakeTable())
    assert store.get("req-1") is None


def test_coordinates_survive_the_real_dynamodb_serializer(store):
    """⚠️ Guards the bug the fake table cannot see.

    Every other test here stubs the table, so boto3's serializer never runs and
    a float sails through. Against the real one it does not: DynamoDB has no
    float type and the resource layer raises rather than rounding. This drives
    the actual TypeSerializer to prove the write would be accepted.
    """
    from boto3.dynamodb.types import TypeSerializer

    table = _use(store, _FakeTable())
    store.create_pending_audio(
        "req-1",
        SESSION_ID,
        {"start": {"latitude": 35.681, "longitude": 139.767}, "elapsedSeconds": 120},
    )

    # Raises TypeError("Float types are not supported") if anything is a float.
    TypeSerializer().serialize(table.item)


def test_coordinates_come_back_as_floats(store):
    """The other half: Decimal must not reach lib/prompt.py.

    prompt.build guards on isinstance(x, (int, float)), so a Decimal reads as
    "no location" and the address and heading are dropped silently - no
    exception, just a worse answer.
    """
    original = {
        "start": {"latitude": 35.681, "longitude": 139.767},
        "elapsedSeconds": 120,
    }
    table = _use(store, _FakeTable())
    store.create_pending_audio("req-1", SESSION_ID, original)

    restored = store.from_dynamo_numbers(table.item["location"])

    assert restored == original
    assert isinstance(restored["start"]["latitude"], float)
    # Whole numbers must stay int: format_elapsed accepts int only.
    assert isinstance(restored["elapsedSeconds"], int)


def test_stored_coordinates_keep_their_value(store):
    """Decimal(str(x)) rather than Decimal(x): the latter stores binary noise."""
    table = _use(store, _FakeTable())
    store.create_pending_audio(
        "req-1", SESSION_ID, {"start": {"latitude": 35.681, "longitude": 139.767}}
    )

    stored = table.item["location"]["start"]
    assert str(stored["latitude"]) == "35.681"
