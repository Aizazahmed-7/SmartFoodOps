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
from aiokafka.errors import TopicAlreadyExistsError

from .serde import AvroSerde


class _KafkaProducer(Protocol):
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def send_and_wait(self, topic: str, value: bytes, *, key: bytes,
                            headers: list[tuple[str, bytes]]) -> Any: ...


class EventProducer:
    def __init__(
        self,
        bootstrap: str,
        serde: AvroSerde,
        *,
        client: _KafkaProducer | None = None,
    ):
        self._serde = serde
        self._client: _KafkaProducer = client or AIOKafkaProducer(
            bootstrap_servers=bootstrap
        )

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
        await self._client.send_and_wait(
            topic, value, key=key.encode(), headers=headers or []
        )


async def ensure_compacted_topic(
    bootstrap: str,
    topic: str,
    *,
    partitions: int = 1,
    admin: Any | None = None,
) -> None:
    """Idempotent: compacted topics never lose the last event per key —
    the compaction-safety contract the full-state payloads rely on."""
    client = admin or AIOKafkaAdminClient(bootstrap_servers=bootstrap)
    await client.start()
    try:
        await client.create_topics(
            [
                NewTopic(
                    name=topic,
                    num_partitions=partitions,
                    replication_factor=1,  # single-broker dev; MSK overrides in prod
                    topic_configs={"cleanup.policy": "compact"},
                )
            ]
        )
    except TopicAlreadyExistsError:
        pass  # the normal case after first boot
    finally:
        await client.close()
