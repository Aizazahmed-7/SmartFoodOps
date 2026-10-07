"""Thin async producer + topic bootstrap.

The aiokafka client is injectable (tests pass a stub); production builds the
real one from the bootstrap address. Keys are aggregate ids — per-aggregate
ordering within a partition, the only ordering the platform promises (§8).
"""

# pyright: reportMissingTypeStubs=false
# (aiokafka ships no py.typed; the _KafkaProducer Protocol below is our typed
# boundary — everything past it is stub-free third-party code.)

from typing import Any, Protocol

from aiokafka import AIOKafkaProducer
from aiokafka.admin import AIOKafkaAdminClient, NewTopic
from aiokafka.admin.config_resource import ConfigResource, ConfigResourceType
from aiokafka.errors import KafkaError, TopicAlreadyExistsError, for_code
from smartfood_otel import get_logger

from .serde import AvroSerde

log = get_logger("smartfood-kafka.producer")


class _KafkaProducer(Protocol):
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def send_and_wait(
        self, topic: str, value: bytes, *, key: bytes, headers: list[tuple[str, bytes]]
    ) -> Any: ...
    async def send(
        self, topic: str, value: bytes, *, key: bytes, headers: list[tuple[str, bytes]]
    ) -> Any: ...


class EventProducer:
    def __init__(
        self,
        bootstrap: str,
        serde: AvroSerde,
        *,
        client: _KafkaProducer | None = None,
    ):
        self._serde = serde
        self._client: _KafkaProducer = client or AIOKafkaProducer(bootstrap_servers=bootstrap)

    async def start(self) -> None:
        await self._client.start()

    async def stop(self) -> None:
        await self._client.stop()

    async def send(
        self,
        topic: str,
        *,
        subject: str,
        schema: dict[str, Any],
        key: str,
        record: dict[str, Any],
        headers: list[tuple[str, bytes]] | None = None,
    ) -> None:
        value = await self._serde.encode(subject, schema, record)
        await self._client.send_and_wait(topic, value, key=key.encode(), headers=headers or [])

    async def send_nowait(
        self,
        topic: str,
        *,
        subject: str,
        schema: dict[str, Any],
        key: str,
        record: dict[str, Any],
        headers: list[tuple[str, bytes]] | None = None,
    ) -> None:
        """Fire-and-forget: append to the client's buffer and return without
        awaiting the broker's ack. For TELEMETRY (browse events) where the
        request being observed must never wait on Kafka — the outbox path
        keeps send() and its ack, because business facts are owed one.
        aiokafka's own bounded buffer is the backpressure: when it is full
        this raises, and the CALLER decides to drop (telemetry always does).
        """
        value = await self._serde.encode(subject, schema, record)
        await self._client.send(topic, value, key=key.encode(), headers=headers or [])


COMPACTED = {"cleanup.policy": "compact"}

_TOPIC_ALREADY_EXISTS = 36
"""Kafka's `TOPIC_ALREADY_EXISTS` error code, as it appears inside a
CreateTopicsResponse rather than as a raised exception."""

_NOT_PERMITTED = frozenset({29, 31})
"""TOPIC_AUTHORIZATION_FAILED and CLUSTER_AUTHORIZATION_FAILED.

These are the NORMAL production posture, not a fault: topics are provisioned
by IaC and the service principal holds produce/consume without CREATE or
ALTER_CONFIGS. Treating them as fatal would make an admin ACL a startup
dependency for a service that only needs to produce — this helper runs
inside catalog's lifespan, so a raise here crash-loops the container."""

_DYNAMIC_TOPIC_CONFIG = 1
"""Kafka's `ConfigSource.DYNAMIC_TOPIC_CONFIG`. Anything else in a describe
response is inherited from the broker, not set on this topic."""


async def ensure_compacted_topic(
    bootstrap: str,
    topic: str,
    *,
    partitions: int = 1,
    admin: Any | None = None,
) -> None:
    """Make the topic exist AND be compacted — on every boot, not just the
    one that happened to create it.

    The earlier version set `cleanup.policy` only inside `create_topics` and
    swallowed `TopicAlreadyExistsError` as "the normal case after first
    boot". That was wrong in a way nothing reported: Kafka auto-creates a
    topic on first produce or subscribe, so whichever client touched it
    first won, and the topic it created inherits the broker defaults —
    `cleanup.policy=delete`, `retention.ms=7 days`. Every later call landed
    in the swallowed branch and changed nothing.

    Found live in B1 (2026-09-16): `c1.catalog.changes` had no topic-level
    config at all, and its earliest and latest offsets were equal — the log
    had aged out completely. That silently voids the guarantee the whole
    full-state payload design is built on: catalog stamps every event with
    the complete menu precisely so the LAST surviving event per key stands
    alone, and identity's grant convergence and the assistant's index
    rebuild (PRD FR-59) both assume something survives. On a delete topic,
    after the retention window, nothing does.

    So: create with the policy, and if the topic is already there, reconcile
    it. The reconcile is a no-op read in the steady state.
    """
    client = admin or AIOKafkaAdminClient(bootstrap_servers=bootstrap)
    await client.start()
    try:
        try:
            response = await client.create_topics(
                [
                    NewTopic(
                        name=topic,
                        num_partitions=partitions,
                        replication_factor=1,  # single-broker dev; MSK overrides in prod
                        topic_configs=dict(COMPACTED),
                    )
                ]
            )
            created = _was_created(response, topic)
        except TopicAlreadyExistsError:
            created = False  # older brokers/clients signal it this way instead
        if created is None:
            log.warning(
                "not permitted to administer topic — leaving its policy as provisioned",
                topic=topic,
            )
            return
        if not created:
            try:
                await _reconcile_compaction(client, topic)
            except KafkaError as exc:
                # Best effort by design. The topic exists and is writable, so
                # the service must start; what is lost is the policy check,
                # and that is worth a loud line rather than an outage.
                log.warning(
                    "could not verify or set topic compaction",
                    topic=topic,
                    error=str(exc),
                )
    finally:
        await client.close()


def _was_created(response: Any, topic: str) -> bool | None:
    """Did THIS call create the topic? `None` = not ours to say.

    The question has to be asked of the RESPONSE, not of an exception.
    aiokafka does not raise for an existing topic on every broker and
    protocol version — `CreateTopicsResponse` carries per-topic error codes,
    and `TOPIC_ALREADY_EXISTS` arrives there as data. The original helper
    caught only the exception, so against a broker that answers this way it
    reported success and changed nothing, on every boot, silently, for the
    life of the topic. That is the actual reason `c1.catalog.changes` spent
    its life on the broker default.

    Three answers, not two. `True` created it, `False` means it was already
    there and should be reconciled, and `None` means the broker refused us
    the administrative right to know — which is a legitimate deployment, not
    a fault (see `_NOT_PERMITTED`).

    Any OTHER error code is re-raised: a topic we could neither create nor
    identify is not something to continue past.
    """
    for entry in getattr(response, "topic_errors", None) or ():
        name, code = entry[0], entry[1]
        if name != topic:
            continue
        if code == _TOPIC_ALREADY_EXISTS:
            return False
        if code in _NOT_PERMITTED:
            return None
        if code:
            raise for_code(code)(f"could not create {topic}: error code {code}")
    return True


async def _reconcile_compaction(client: Any, topic: str) -> None:
    """Bring an existing topic's `cleanup.policy` to `compact`.

    Reads before it writes, for two reasons. The cheap one: in the steady
    state this is every boot of every service that owns a topic, and a
    describe is cheaper than a needless config write. The important one:
    `alter_configs` is NOT incremental — it replaces the resource's entire
    dynamic config set — so anything an operator set on this topic by hand
    would be silently reverted to the broker default. Carrying forward every
    entry whose source is `DYNAMIC_TOPIC_CONFIG` keeps a correction from
    becoming a regression.
    """
    described = await client.describe_configs([ConfigResource(ConfigResourceType.TOPIC, topic)])
    entries = described[0].resources[0][4]
    current = {name: (value, source) for name, value, _read_only, source, *_ in entries}
    policy = current.get("cleanup.policy", ("", 0))[0] or ""
    if "compact" in policy:
        return
    configs = {
        name: value for name, (value, source) in current.items() if source == _DYNAMIC_TOPIC_CONFIG
    }
    configs.update(COMPACTED)
    await client.alter_configs([ConfigResource(ConfigResourceType.TOPIC, topic, configs=configs)])
    log.warning(
        "topic was not compacted — corrected",
        topic=topic,
        previous_policy=policy or "(broker default)",
    )
