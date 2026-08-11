"""Temporal worker — skeleton. Connects to nothing yet; OrderWorkflow lands
in the saga step (S5), which rewrites this file and removes the pragma."""

import asyncio
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
log = logging.getLogger("order.worker")


async def main() -> None:  # pragma: no cover — skeleton keep-alive, replaced in S5
    log.info("order-worker skeleton up — no workflows registered yet")
    while True:
        await asyncio.sleep(3600)


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(main())
