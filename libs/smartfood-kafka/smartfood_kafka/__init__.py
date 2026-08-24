"""smartfood-kafka — Avro serde, Schema Registry, producer, and consumer.

The mandatory path for anything touching Kafka (docs §8): no service builds
its own serializer or consumer loop, so wire-format, compatibility, and
at-least-once discipline have one implementation to audit. Test doubles
live in `smartfood_kafka.testing` (imported explicitly, never from here).
"""

from .consumer import BatchHandler, EventConsumer, EventHandler
from .envelope import DOMAIN_EVENT_SCHEMA, DOMAIN_EVENT_SUBJECT
from .producer import EventProducer, ensure_compacted_topic
from .registry import SchemaRegistry, SchemaRegistryError
from .serde import AvroSerde, SerdeError
from .vocabulary import EventType, Topic, topic

__all__ = [
    "DOMAIN_EVENT_SCHEMA",
    "DOMAIN_EVENT_SUBJECT",
    "AvroSerde",
    "SerdeError",
    "SchemaRegistry",
    "SchemaRegistryError",
    "BatchHandler",
    "EventConsumer",
    "EventHandler",
    "EventProducer",
    "EventType",
    "Topic",
    "ensure_compacted_topic",
    "topic",
]
