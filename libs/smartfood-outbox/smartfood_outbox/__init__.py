"""smartfood-outbox — outbox event identity (the poller/drain lands in W3).

Every service stages outbox rows in the same transaction as its writes
(ARCHITECTURE §invariant 1); this module owns the one rule all of them share:
deterministic event ids (ADR-0018), so a retried transaction or a re-run
poller can never mint a second identity for the same fact.
"""

import uuid

# Fixed project namespace — NEVER change it: event ids must stay stable
# across deploys, or every consumer's dedupe store silently resets.
_NAMESPACE = uuid.UUID("b6a7e2d4-9c41-4f5a-8f37-2f8f0a6e1c93")


def event_id(aggregate_type: str, aggregate_id: str, version: int, event_type: str) -> str:
    """UUIDv5 of aggregate:{id}:{version}:{type} — same fact, same id, always."""
    return str(uuid.uuid5(_NAMESPACE, f"{aggregate_type}:{aggregate_id}:{version}:{event_type}"))
