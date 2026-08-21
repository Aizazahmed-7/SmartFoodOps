"""DLQ replay — the other half of ADR-0021's contract.

Parking says "a human will look"; this is what the human runs after the
fix ships:  make dlq-replay TOPIC=c1.orders.events.dlq

Semantics, chosen to match the platform's at-least-once spine:
- Republishes each parked event to its `dlq.source.topic` with the ORIGINAL
  key and headers, minus the `dlq.*` forensics (a replayed event that fails
  again gets freshly stamped ones). The original traceparent survives, so
  the replayed consumption chains onto the trace that produced the event.
- SerdeError parks (attempts=0, undecodable bytes) are SKIPPED: replaying
  them can only re-park them — the runbook sends you to the producer.
- Offsets commit after each drained batch: a crash mid-batch re-replays the
  batch, and consumer dedupe (deterministic event ids) absorbs the repeats.
  The consumer group ("dlq-replay") remembers progress, so re-running the
  tool replays only newly-parked events.
- Drains until caught up, then EXITS — a tool, not a daemon.
"""

import asyncio
import os
import sys
from typing import Any

from aiokafka import (  # pyright: ignore[reportMissingTypeStubs]
    AIOKafkaConsumer,
    AIOKafkaProducer,
)
from smartfood_otel import get_logger

log = get_logger("smartfood-kafka.replay")

GROUP = "dlq-replay"


class DlqReplayer:
    def __init__(
        self,
        dlq_topic: str,
        *,
        bootstrap: str = "localhost:19092",
        client: Any | None = None,  # test seams, same pattern as EventConsumer
        producer: Any | None = None,
    ):
        self._dlq_topic = dlq_topic
        self._bootstrap = bootstrap
        self._client = client
        self._producer = producer

    def _make_client(self) -> Any:
        if self._client is not None:
            return self._client
        return AIOKafkaConsumer(  # pragma: no cover — live wiring
            self._dlq_topic,
            bootstrap_servers=self._bootstrap,
            group_id=GROUP,
            enable_auto_commit=False,
            auto_offset_reset="earliest",
        )

    def _make_producer(self) -> Any:
        if self._producer is not None:
            return self._producer
        return AIOKafkaProducer(bootstrap_servers=self._bootstrap)  # pragma: no cover

    async def replay_once(self) -> tuple[int, int]:
        """Drain everything currently parked; returns (replayed, skipped)."""
        producer = self._make_producer()
        await producer.start()
        try:
            client = self._make_client()
            await client.start()
            try:
                replayed = skipped = 0
                while True:
                    batches = await client.getmany(timeout_ms=2000)
                    messages = [m for batch in batches.values() for m in batch]
                    if not messages:
                        return replayed, skipped
                    for message in messages:
                        headers: list[tuple[str, bytes | None]] = list(message.headers or [])
                        by_key = {k: v for k, v in headers if v is not None}
                        source = by_key.get("dlq.source.topic")
                        error_type = (by_key.get("dlq.error.type") or b"").decode(errors="replace")
                        if source is None:
                            skipped += 1
                            log.error(
                                "parked event carries no dlq.source.topic — cannot replay",
                                offset=message.offset,
                            )
                        elif error_type == "SerdeError":
                            skipped += 1
                            log.warning(
                                "undecodable original — replay would only re-park it",
                                offset=message.offset,
                            )
                        else:
                            clean = [(k, v) for k, v in headers if not k.startswith("dlq.")]
                            await producer.send_and_wait(
                                source.decode(), message.value, key=message.key, headers=clean
                            )
                            replayed += 1
                            log.info(
                                "event replayed",
                                target=source.decode(),
                                source_offset=message.offset,
                            )
                    # Commit AFTER the batch is republished: a crash here
                    # re-replays the batch; dedupe absorbs it (module doc).
                    await client.commit()
            finally:
                await client.stop()
        finally:
            await producer.stop()


def main() -> None:  # pragma: no cover — live CLI (make dlq-replay)
    topic = sys.argv[1]
    replayer = DlqReplayer(topic, bootstrap=os.environ.get("KAFKA_BOOTSTRAP", "localhost:19092"))
    replayed, skipped = asyncio.run(replayer.replay_once())
    print(f"replayed={replayed} skipped={skipped} (skips: see logs — serde/poison stay parked)")


if __name__ == "__main__":  # pragma: no cover
    main()
