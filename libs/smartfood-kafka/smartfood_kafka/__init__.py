"""smartfood-kafka — Avro serde, Schema Registry, and the event producer.

The mandatory path for anything touching Kafka (docs §8): no service builds
its own serializer, so wire-format and compatibility discipline have one
implementation to audit.
"""

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
    "EventProducer",
    "EventType",
    "Topic",
    "ensure_compacted_topic",
    "topic",
]
