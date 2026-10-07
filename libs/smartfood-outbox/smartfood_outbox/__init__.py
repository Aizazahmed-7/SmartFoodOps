"""smartfood-outbox — the outbox table, its staging call, and the poller.

Every service stages outbox rows in the same transaction as its writes
(ARCHITECTURE §invariant 1). Event ids are random uuid4 as of ADR-0035:
the deterministic UUIDv5 they replaced (ADR-0018) never deduplicated
anything, because every staging site is already guarded by its own
aggregate's uniqueness before the event row is inserted.
"""

from .poller import OutboxPoller
from .table import outbox_table, stage_event

__all__ = ["OutboxPoller", "outbox_table", "stage_event"]
