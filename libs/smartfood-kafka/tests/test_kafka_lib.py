"""Every branch of the kafka lib, offline: registry caching + errors, the
Confluent wire format roundtrip, producer send path, topic bootstrap."""

import json
import struct
from datetime import UTC, datetime

import httpx
import pytest
from aiokafka.errors import ClusterAuthorizationFailedError, KafkaError, TopicAlreadyExistsError
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

    async def send(self, topic, value, *, key, headers):
        # aiokafka's fire-and-forget: buffer-append, ack future discarded.
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


async def test_send_nowait_appends_without_awaiting_an_ack():
    """The telemetry path: same wire format, same registry framing — the
    only difference is WHICH client call carries it (buffered send, not
    send_and_wait), so a slow broker can never stall the observed request."""
    registry, _ = make_registry(schema_id=9)
    stub = StubKafka()
    producer = EventProducer("kafka:9092", AvroSerde(registry), client=stub)
    await producer.start()
    await producer.send_nowait(
        "c1.browse.events",
        subject=DOMAIN_EVENT_SUBJECT,
        schema=DOMAIN_EVENT_SCHEMA,
        key="rst_1",
        record=EVENT,
    )
    await producer.stop()
    topic, value, key, headers = stub.sent[0]
    assert (topic, key, headers) == ("c1.browse.events", b"rst_1", [])
    assert value[0] == 0 and struct.unpack(">I", value[1:5])[0] == 9


class _CreateResponse:
    def __init__(self, topic_errors):
        self.topic_errors = topic_errors


class _Described:
    """The shape aiokafka hands back: response.resources[0] is a 5-tuple
    whose last element is a list of (name, value, read_only, source, ...)."""

    def __init__(self, entries):
        self.resources = [(0, "", 2, "topic", entries)]


class StubAdmin:
    DEFAULT = 5  # ConfigSource.DEFAULT_CONFIG — inherited from the broker
    DYNAMIC = 1  # ConfigSource.DYNAMIC_TOPIC_CONFIG — set on this topic

    def __init__(self, exists: bool = False, entries: list | None = None, raises: bool = False):
        self.exists = exists
        self.raises = raises
        self.created: list = []
        self.altered: list = []
        self.closed = False
        # What a topic Kafka auto-created looks like: policy inherited.
        self._entries = (
            entries
            if entries is not None
            else [("cleanup.policy", "delete", False, self.DEFAULT, False, [])]
        )

    async def start(self):
        pass

    async def create_topics(self, topics):
        if self.raises:
            raise TopicAlreadyExistsError
        if self.exists:
            # How a live broker actually answers: the error rides IN the
            # response, which is the blind spot that let this go unnoticed.
            return _CreateResponse([(t.name, 36, "already exists") for t in topics])
        self.created.extend(topics)
        return _CreateResponse([(t.name, 0, None) for t in topics])

    async def describe_configs(self, resources):
        return [_Described(self._entries)]

    async def alter_configs(self, resources):
        self.altered.extend(resources)

    async def close(self):
        self.closed = True


async def test_ensure_compacted_topic_creates_with_the_policy():
    admin = StubAdmin()
    await ensure_compacted_topic("kafka:9092", "c1.catalog.changes", admin=admin)
    assert admin.created[0].name == "c1.catalog.changes"
    assert admin.created[0].topic_configs == {"cleanup.policy": "compact"}
    assert admin.altered == [] and admin.closed


async def test_an_existing_topic_that_is_already_compacted_is_left_alone():
    """The steady state — every boot of every service that owns a topic. A
    describe and nothing else."""
    admin = StubAdmin(
        exists=True,
        entries=[("cleanup.policy", "compact", False, StubAdmin.DYNAMIC, False, [])],
    )
    await ensure_compacted_topic("kafka:9092", "c1.catalog.changes", admin=admin)
    assert admin.created == [] and admin.altered == [] and admin.closed


async def test_an_already_exists_code_in_the_response_is_not_mistaken_for_success():
    """THE root cause. aiokafka does not raise for an existing topic on
    every broker version — TOPIC_ALREADY_EXISTS (36) arrives inside
    CreateTopicsResponse. The original helper caught only the exception, so
    against a broker that answers this way it reported success and changed
    nothing, on every boot, for the life of the topic."""
    admin = StubAdmin(exists=True)
    await ensure_compacted_topic("kafka:9092", "c1.catalog.changes", admin=admin)
    assert admin.altered[0].configs["cleanup.policy"] == "compact"


async def test_the_raising_broker_path_still_reconciles():
    """Older clients/brokers signal it as an exception. Both roads lead to
    the same reconcile."""
    admin = StubAdmin(exists=True, raises=True)
    await ensure_compacted_topic("kafka:9092", "c1.catalog.changes", admin=admin)
    assert admin.altered[0].configs["cleanup.policy"] == "compact"


async def test_another_topics_error_is_not_read_as_ours():
    """A response may carry entries we did not ask about. Matching on the
    name keeps a neighbour's failure from being reported as this topic's —
    the difference between "created" and "raise" for the wrong reason."""

    class Noisy(StubAdmin):
        async def create_topics(self, topics):
            return _CreateResponse(
                [("some.other.topic", 41, "not controller"), ("c1.catalog.changes", 0, None)]
            )

    admin = Noisy()
    await ensure_compacted_topic("kafka:9092", "c1.catalog.changes", admin=admin)
    assert admin.altered == []  # ours was created, so nothing to reconcile


async def test_a_topic_kafka_auto_created_is_corrected():
    """THE regression. Kafka auto-creates on first produce or subscribe with
    the broker defaults (delete, 7-day retention); the old code swallowed
    TopicAlreadyExistsError and left it that way forever. Found live in B1:
    c1.catalog.changes had aged out entirely, which voids the
    last-event-per-key guarantee the full-state payloads are built on."""
    admin = StubAdmin(exists=True, raises=True)  # policy inherited from the broker
    await ensure_compacted_topic("kafka:9092", "c1.catalog.changes", admin=admin)
    assert admin.created == []
    (resource,) = admin.altered
    assert resource.name == "c1.catalog.changes"
    assert resource.configs["cleanup.policy"] == "compact"
    assert admin.closed


async def test_correcting_the_policy_preserves_an_operators_own_overrides():
    """`alter_configs` REPLACES the resource's dynamic set, so a correction
    that did not carry these forward would silently revert them to the
    broker default — turning a fix into a regression."""
    admin = StubAdmin(
        exists=True,
        entries=[
            ("cleanup.policy", "delete", False, StubAdmin.DEFAULT, False, []),
            ("max.message.bytes", "2097152", False, StubAdmin.DYNAMIC, False, []),
            ("retention.ms", "604800000", False, StubAdmin.DEFAULT, False, []),
        ],
    )
    await ensure_compacted_topic("kafka:9092", "c1.catalog.changes", admin=admin)
    (resource,) = admin.altered
    assert resource.configs["max.message.bytes"] == "2097152"  # operator's, kept
    assert resource.configs["cleanup.policy"] == "compact"  # ours, applied
    assert "retention.ms" not in resource.configs  # inherited, not ours to pin


async def test_a_topic_with_no_policy_entry_at_all_is_corrected():
    """Defensive: a broker that omits the key rather than reporting a
    default must not be read as "already compacted"."""
    admin = StubAdmin(exists=True, entries=[])
    await ensure_compacted_topic("kafka:9092", "c1.catalog.changes", admin=admin)
    assert admin.altered[0].configs == {"cleanup.policy": "compact"}


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


async def test_a_topic_we_may_not_administer_does_not_block_startup():
    """The normal production posture: topics provisioned by IaC, the service
    principal holding produce/consume and nothing else. This helper runs
    inside catalog's lifespan, so raising here would crash-loop a container
    that only ever needed to produce."""

    class Forbidden(StubAdmin):
        async def create_topics(self, topics):
            return _CreateResponse([("c1.catalog.changes", 29, "not authorized")])

    admin = Forbidden()
    await ensure_compacted_topic("kafka:9092", "c1.catalog.changes", admin=admin)
    assert admin.altered == []  # not attempted — we were told we may not
    assert admin.closed


async def test_a_reconcile_we_may_not_perform_does_not_block_startup():
    """Create may be permitted while ALTER_CONFIGS is not. The topic exists
    and is writable; what is lost is the policy check, which is worth a loud
    log line and not an outage."""

    class NoAlter(StubAdmin):
        async def alter_configs(self, resources):
            raise ClusterAuthorizationFailedError("nope")

    admin = NoAlter(exists=True)
    await ensure_compacted_topic("kafka:9092", "c1.catalog.changes", admin=admin)
    assert admin.closed


async def test_an_unrecognised_failure_is_still_raised():
    """The line between "not permitted" and "broken" has to stay somewhere:
    a topic we could neither create nor identify is not something to
    continue past."""

    class NotController(StubAdmin):
        async def create_topics(self, topics):
            return _CreateResponse([("c1.catalog.changes", 41, "not controller")])

    with pytest.raises(KafkaError):
        await ensure_compacted_topic("kafka:9092", "c1.catalog.changes", admin=NotController())
