"""smartfood-idempotency — the api-standards §4 protocol, once.

Order placement and payment's money operations run the same audited
reserve/replay/reuse machinery against their OWN tables in their OWN
databases (the row must join the service's business transaction)."""

from .janitor import IdempotencyJanitor
from .store import (
    BodyMismatch,
    IdempotencyStore,
    InProgress,
    Replay,
    Reserved,
    ReserveOutcome,
    body_hash,
)
from .table import idempotency_table

__all__ = [
    "BodyMismatch",
    "IdempotencyJanitor",
    "IdempotencyStore",
    "InProgress",
    "Replay",
    "Reserved",
    "ReserveOutcome",
    "body_hash",
    "idempotency_table",
]
