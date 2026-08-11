"""The DomainEvent envelope — ARCHITECTURE §8's contract as an Avro record.

One envelope for every domain topic: strongly-typed metadata, with the
full-state snapshot as a JSON string payload (menus are documents; modeling
them field-by-field in Avro would couple every consumer to catalog's shape).
`traceparent` deliberately travels in Kafka HEADERS, not the schema — it is
transport context, not a fact about the aggregate (docs §12).

Subjects follow RecordNameStrategy (docs §8): the subject IS the record's
full name, so every topic carrying DomainEvent shares one compatibility
lineage, gated BACKWARD_TRANSITIVE at the registry.
"""

from typing import Any

DOMAIN_EVENT_SCHEMA: dict[str, Any] = {
    "type": "record",
    "name": "DomainEvent",
    "namespace": "smartfoodops.events.v1",
    "fields": [
        {"name": "event_id", "type": "string", "doc": "deterministic UUIDv5 (ADR-0018)"},
        {"name": "event_type", "type": "string"},
        {"name": "aggregate_type", "type": "string"},
        {"name": "aggregate_id", "type": "string"},
        {"name": "aggregate_version", "type": "long"},
        {"name": "occurred_at", "type": {"type": "long", "logicalType": "timestamp-micros"}},
        {"name": "cell_id", "type": "string"},
        {"name": "payload", "type": "string", "doc": "JSON-encoded full-state snapshot"},
    ],
}

# RecordNameStrategy: subject == record full name.
DOMAIN_EVENT_SUBJECT = f"{DOMAIN_EVENT_SCHEMA['namespace']}.{DOMAIN_EVENT_SCHEMA['name']}"
