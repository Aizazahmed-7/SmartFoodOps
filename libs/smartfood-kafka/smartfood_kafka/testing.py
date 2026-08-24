"""Test doubles for the consumer runtime — shipped as product.

Every consumer suite needs the same three stand-ins (client, serde,
handler); identity and inventory each hand-rolled them before the runtime
was extracted. Importing these keeps a service's tests about its HANDLER,
not about re-describing the aiokafka surface.
"""

import json
from dataclasses import dataclass
from typing import Any

from .serde import SerdeError


@dataclass
class StubMessage:
    """The slice of aiokafka's ConsumerRecord the runtime reads. Header
    values are bytes | None on the wire — Kafka permits null values."""

    value: bytes
    headers: list[tuple[str, Any]] | None = None
    topic: str = "c1.test.events"
    partition: int = 0
    offset: int = 0
    key: bytes | None = None


class StubKafkaConsumer:
    """aiokafka-shaped consumer over a fixed message list. Each __aiter__
    replays the list from the start — exactly what an uncommitted offset
    does on a real rejoin, so supervision tests get redelivery for free."""

    def __init__(self, messages: list[StubMessage]):
        self._messages = messages
        self.starts = 0
        self.stopped = False
        self.commits = 0
        self.fail_commits = 0  # script N commit failures (at-least-once tests)
        self._drained = False

    async def start(self) -> None:
        self.starts += 1
        self.stopped = False

    async def stop(self) -> None:
        self.stopped = True

    async def commit(self) -> None:
        if self.fail_commits > 0:
            self.fail_commits -= 1
            raise RuntimeError("commit failed")
        self.commits += 1

    async def getmany(
        self, *partitions: object, timeout_ms: int = 0, max_records: int | None = None
    ) -> dict[str, list[StubMessage]]:
        """Bounded read for the DLQ replayer: everything once, then empty —
        what a caught-up consumer group looks like."""
        if self._drained:
            return {}
        self._drained = True
        return {"tp-0": list(self._messages)} if self._messages else {}

    def __aiter__(self) -> Any:
        async def gen() -> Any:
            for message in self._messages:
                yield message

        return gen()


class StubSerde:
    """JSON stand-in for the Avro serde: tests write payloads as plain
    dicts. Honors the Decoder contract — garbage bytes raise SerdeError
    (the poison path), exactly like the real serde."""

    async def decode(self, data: bytes) -> dict[str, Any]:
        try:
            decoded: dict[str, Any] = json.loads(data)
        except ValueError as exc:
            raise SerdeError("not valid JSON") from exc
        return decoded


class RecordingHandler:
    """Records handled events; scripted to fail its first N calls so retry
    and DLQ branches are one constructor argument away."""

    def __init__(self, fail_times: int = 0):
        self.fail_times = fail_times
        self.events: list[dict[str, Any]] = []
        self.attempts = 0

    async def handle(self, event: dict[str, Any]) -> None:
        self.attempts += 1
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("handler boom")
        self.events.append(event)


class StubDlq:
    """Records parked messages as (topic, value, key, headers) tuples."""

    def __init__(self, fail_times: int = 0):
        self.fail_times = fail_times
        self.started = False
        self.stopped = False
        self.parked: list[tuple[str, bytes, bytes | None, list[tuple[str, bytes]]]] = []

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    async def send_and_wait(
        self, topic: str, value: bytes, *, key: bytes | None, headers: list[tuple[str, bytes]]
    ) -> Any:
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError("dlq down")
        self.parked.append((topic, value, key, headers))
