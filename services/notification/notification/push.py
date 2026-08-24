"""Bell push — the per-recipient hint seam (S9).

The order-tracking pattern, third verse: rows are the record, the stream
is a HINT ("look again"), the FE refetches and renders only what the GET
returns. Therefore the bus fails OPEN and lives entirely off the inbox
write path — a lost hint costs at most one poll interval of staleness.

Channels are per RECIPIENT, mirroring exactly how the read side scopes
(`_recipient`): customers get sfo:notify:customer:{sub}, owners get
sfo:notify:restaurant:{rid} — one stream per signed-in identity.
"""

from typing import Protocol

from smartfood_otel import get_logger

log = get_logger("notification.push")


class HintPublisher(Protocol):
    async def publish(self, channel: str, data: str) -> None: ...


def notify_channel(recipient_type: str, recipient_id: str) -> str:
    """The one place the bell channel name is spelled."""
    return f"sfo:notify:{recipient_type}:{recipient_id}"


_publisher: HintPublisher | None = None


def set_publisher(publisher: HintPublisher | None) -> None:
    global _publisher
    _publisher = publisher


def reset_publisher() -> None:
    set_publisher(None)


async def publish_hint(recipient_type: str, recipient_id: str) -> None:
    """Fire the hint; never let the hint break the inbox write it follows."""
    if _publisher is None:
        return
    try:
        await _publisher.publish(notify_channel(recipient_type, recipient_id), recipient_type)
    except Exception as exc:
        log.warning(
            "bell hint dropped — the poll floor catches up",
            recipient_type=recipient_type,
            error=str(exc),
        )
