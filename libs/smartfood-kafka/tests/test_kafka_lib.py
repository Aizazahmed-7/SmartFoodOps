"""Every branch of the kafka lib, offline: registry caching + errors, the
Confluent wire format roundtrip, producer send path, topic bootstrap."""

import json
import struct
from datetime import UTC, datetime

import httpx
import pytest
from aiokafka.errors import TopicAlreadyExistsError
from smartfood_kafka import (
    DOMAIN_EVENT_SCHEMA,
    DOMAIN_EVENT_SUBJECT,
    AvroSerde,
    EventProducer,
    SchemaRegistry,
    SchemaRegistryError,
    SerdeError,
    ensure_compacted_topic,
)

EVENT = {
    "event_id": "e-1",
    "event_type": "RestaurantCreated",
    "aggregate_type": "restaurant",
    "aggregate_id": "rst_1",
    "aggregate_version": 1,
    "occurred_at": datetime(2026, 8, 10, 12, 0, tzinfo=UTC),
    "cell_id": "c1",
    "payload": json.dumps({"owner_user_id": "usr_1"}),
}


def make_registry(schema_id: int = 7, fail: bool = False):
    calls = {"n": 0, "paths": []}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        calls["paths"].append(request.url.path)
        if fail:
            return httpx.Response(500, text="registry down")
        if request.url.path.startswith("/config/"):
            return httpx.Response(200, json={"compatibility": "BACKWARD_TRANSITIVE"})
        if request.url.path.endswith("/versions"):
            return httpx.Response(200, json={"id": schema_id})
        if "/schemas/ids/" in request.url.path:
            return httpx.Response(200, json={"schema": json.dumps(DOMAIN_EVENT_SCHEMA)})
        return httpx.Response(404)

    registry = SchemaRegistry(
        "http://sr.test", httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    return registry, calls


async def test_register_sets_compatibility_then_caches():
    registry, calls = make_registry()
    assert await registry.register(DOMAIN_EVENT_SUBJECT, DOMAIN_EVENT_SCHEMA) == 7
    assert calls["paths"][0] == f"/config/{DOMAIN_EVENT_SUBJECT}"  # BACKWARD_TRANSITIVE first
    assert await registry.register(DOMAIN_EVENT_SUBJECT, DOMAIN_EVENT_SCHEMA) == 7
    assert calls["n"] == 2  # cached — no third call


async def test_registry_errors_are_loud():
    registry, _ = make_registry(fail=True)
    with pytest.raises(SchemaRegistryError):
        await registry.register(DOMAIN_EVENT_SUBJECT, DOMAIN_EVENT_SCHEMA)
    with pytest.raises(SchemaRegistryError):
        await registry.schema_by_id(99)


async def test_wire_format_roundtrip():
    registry, calls = make_registry(schema_id=42)
    serde = AvroSerde(registry)
    wire = await serde.encode(DOMAIN_EVENT_SUBJECT, DOMAIN_EVENT_SCHEMA, EVENT)
    assert wire[0] == 0  # Confluent magic byte
    assert struct.unpack(">I", wire[1:5])[0] == 42  # embedded schema id

    # A FRESH serde (consumer side) must resolve the schema by id:
    consumer_registry, consumer_calls = make_registry(schema_id=42)
    decoded = await AvroSerde(consumer_registry).decode(wire)
    assert decoded == EVENT
    assert "/schemas/ids/42" in consumer_calls["paths"][0]

    # Same-serde decode reuses the parse cache — no registry hit:
    before = calls["n"]
    assert (await serde.decode(wire))["event_id"] == "e-1"
    assert calls["n"] == before


async def test_decode_rejects_garbage():
    registry, _ = make_registry()
    serde = AvroSerde(registry)
    with pytest.raises(SerdeError):
        await serde.decode(b"\x01junk-not-wire-format")
    with pytest.raises(SerdeError):
        await serde.decode(b"\x00\x00")  # too short


async def test_corrupt_avro_body_is_serde_error_not_transport():
    """A well-formed header over a mangled body must classify as POISON
    (SerdeError → DLQ), while registry failures stay transport errors —
    the consumer parks only what replay can never fix."""
    registry, _ = make_registry(schema_id=7)
    serde = AvroSerde(registry)
    wire = await serde.encode(DOMAIN_EVENT_SUBJECT, DOMAIN_EVENT_SCHEMA, EVENT)
    with pytest.raises(SerdeError, match="undecodable Avro body"):
        await serde.decode(wire[:5] + b"\x9f\x9f\x9f")  # header intact, body mangled


class StubKafka:
    def __init__(self):
        self.started = self.stopped = False
        self.sent: list[tuple] = []

    async def start(self):
        self.started = True

    async def stop(self):
        self.stopped = True

    async def send_and_wait(self, topic, value, *, key, headers):
        self.sent.append((topic, value, key, headers))


async def test_producer_sends_wire_format_with_key_and_headers():
    registry, _ = make_registry(schema_id=9)
    stub = StubKafka()
    producer = EventProducer("kafka:9092", AvroSerde(registry), client=stub)
    await producer.start()
    await producer.send(
        "c1.catalog.changes",
        subject=DOMAIN_EVENT_SUBJECT,
        schema=DOMAIN_EVENT_SCHEMA,
        key="rst_1",
        record=EVENT,
        headers=[("traceparent", b"00-abc-def-01")],
    )
    await producer.stop()
    assert stub.started and stub.stopped
    topic, value, key, headers = stub.sent[0]
    assert (topic, key) == ("c1.catalog.changes", b"rst_1")
    assert value[0] == 0 and struct.unpack(">I", value[1:5])[0] == 9
    assert headers == [("traceparent", b"00-abc-def-01")]


class StubAdmin:
    def __init__(self, exists: bool = False):
        self.exists = exists
        self.created: list = []
        self.closed = False

    async def start(self):
        pass

    async def create_topics(self, topics):
        if self.exists:
            raise TopicAlreadyExistsError
        self.created.extend(topics)

    async def close(self):
        self.closed = True


async def test_ensure_compacted_topic_creates_once():
    admin = StubAdmin()
    await ensure_compacted_topic("kafka:9092", "c1.catalog.changes", admin=admin)
    assert admin.created[0].name == "c1.catalog.changes"
    assert admin.created[0].topic_configs == {"cleanup.policy": "compact"}
    assert admin.closed

    replay = StubAdmin(exists=True)
    await ensure_compacted_topic("kafka:9092", "c1.catalog.changes", admin=replay)
    assert replay.created == [] and replay.closed  # swallowed, admin still closed


async def test_versions_endpoint_refusal_is_loud():
    """Compat check passes but the schema itself is refused (incompatible)."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/config/"):
            return httpx.Response(200, json={})
        return httpx.Response(409, text="incompatible schema")

    registry = SchemaRegistry(
        "http://sr.test", httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    with pytest.raises(SchemaRegistryError):
        await registry.register(DOMAIN_EVENT_SUBJECT, DOMAIN_EVENT_SCHEMA)


async def test_schema_by_id_caches():
    registry, calls = make_registry()
    await registry.schema_by_id(7)
    await registry.schema_by_id(7)
    assert calls["n"] == 1  # second hit served from cache
