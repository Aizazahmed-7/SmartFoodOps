"""The DLQ replayer: republish semantics, skip rules, commit discipline."""

import json

from smartfood_kafka.replay import DlqReplayer
from smartfood_kafka.testing import StubDlq, StubKafkaConsumer, StubMessage


def _parked(
    event_id: str,
    *,
    source: bytes | None = b"c1.orders.events",
    error: bytes = b"RuntimeError",
    extra: tuple = (),
):
    """A message as _park writes it: original bytes + original headers +
    dlq.* forensics."""
    headers = [
        ("traceparent", b"00-" + b"ab" * 16 + b"-" + b"cd" * 8 + b"-01"),
        *extra,
        ("dlq.error.type", error),
        ("dlq.error.message", b"boom"),
        *((("dlq.source.topic", source),) if source is not None else ()),
        ("dlq.attempts", b"5"),
    ]
    return StubMessage(value=json.dumps({"event_id": event_id}).encode(), headers=headers)


def _replayer(messages):
    client = StubKafkaConsumer(messages)
    producer = StubDlq()
    return DlqReplayer("c1.orders.events.dlq", client=client, producer=producer), client, producer


async def test_replays_to_the_source_topic_with_forensics_stripped():
    replayer, client, producer = _replayer([_parked("e1"), _parked("e2")])
    assert await replayer.replay_once() == (2, 0)

    assert len(producer.parked) == 2
    topic, value, key, headers = producer.parked[0]
    assert topic == "c1.orders.events"  # back to where it came from
    assert json.loads(value)["event_id"] == "e1"  # ORIGINAL bytes, verbatim
    header_keys = [k for k, _ in headers]
    assert not any(k.startswith("dlq.") for k in header_keys)  # forensics stripped
    assert "traceparent" in header_keys  # the original trace survives
    assert client.commits == 1  # committed AFTER the batch
    assert client.stopped and producer.stopped


async def test_serde_parks_are_skipped_not_recycled():
    """Undecodable bytes re-park forever — the replayer refuses the cycle
    and leaves them committed-past (the runbook sends you to the producer)."""
    replayer, client, producer = _replayer([_parked("e1", error=b"SerdeError"), _parked("e2")])
    assert await replayer.replay_once() == (1, 1)
    assert len(producer.parked) == 1
    assert client.commits == 1  # the skip is still committed — never re-seen


async def test_missing_source_topic_is_skipped_loudly():
    replayer, _, producer = _replayer([_parked("e1", source=None)])
    assert await replayer.replay_once() == (0, 1)
    assert producer.parked == []


async def test_caught_up_topic_replays_nothing():
    replayer, client, producer = _replayer([])
    assert await replayer.replay_once() == (0, 0)
    assert producer.parked == [] and client.stopped
