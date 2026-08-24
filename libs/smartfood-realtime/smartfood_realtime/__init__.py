"""Realtime plumbing shared by every SSE lane (S4 order tracking, S9 the
notification bell, dispatch next): single-use channel tickets over Redis,
the pub/sub bus, and the stream generator with heartbeats and the jittered
lifetime.

Extracted from services/order at its SECOND consumer (the rule of three,
called early because the third — dispatch — is already on the roadmap).

The one generalization made during the lift: a ticket authorizes a
CHANNEL, not an order. The claim stores the fully-qualified channel name
("sfo:track:ord_42", "sfo:notify:customer:usr_1"), and each stream
endpoint checks the claim against the channel it is about to serve — so a
tracking ticket redeemed against the bell (or vice versa) fails
structurally, not by convention.
"""

from .bus import RedisRealtime, Subscription
from .stream import StreamConfig, sse_event, stream_events

__all__ = [
    "RedisRealtime",
    "StreamConfig",
    "Subscription",
    "sse_event",
    "stream_events",
]
