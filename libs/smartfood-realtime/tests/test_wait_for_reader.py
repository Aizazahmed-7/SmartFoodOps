"""Waiting for a reader before speaking (the assistant's token stream).

The bus is pub/sub: a frame published into a channel with no subscriber is
dropped, not queued. For a live hint that is correct — the next hint repairs
it. For a token stream it is silent loss, and the worst case is the one that
looks safest: a safety refusal and an empty retrieval are answered with no
model at all, so the WHOLE answer is published within a millisecond of the
turn starting, while the browser is still reading the 202 that told it which
channel to open.
"""

import asyncio

import pytest
from smartfood_realtime import wait_for_reader

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class Bus:
    """Counts subscribers, and can gain one after N polls."""

    def __init__(self, *, attaches_after: int | None = 0) -> None:
        self._after = attaches_after
        self.polls = 0

    async def readers(self, channel: str) -> int:
        del channel
        self.polls += 1
        if self._after is None:
            return 0
        return 1 if self.polls > self._after else 0


async def test_a_reader_already_attached_costs_nothing():
    """The common path. A generated answer spends hundreds of milliseconds
    in retrieval and the provider before its first token, so the reader has
    long since arrived — this must not add a poll interval to every turn."""
    bus = Bus(attaches_after=0)
    assert await wait_for_reader(bus, "sfo:assist:msg_1", timeout_s=2.0) is True
    assert bus.polls == 1  # asked once, answered yes, returned


async def test_a_reader_that_arrives_late_is_waited_for():
    bus = Bus(attaches_after=3)
    assert await wait_for_reader(bus, "sfo:assist:msg_1", timeout_s=2.0, poll_s=0.001) is True
    assert bus.polls == 4


async def test_a_reader_that_never_comes_gives_up_rather_than_hanging():
    """A client that closed the tab must not pin a turn open forever. The
    turn runs anyway: the durable answer is the `messages` row, which is
    written whether or not anybody was listening."""
    bus = Bus(attaches_after=None)
    assert await wait_for_reader(bus, "sfo:assist:msg_1", timeout_s=0.05, poll_s=0.001) is False


async def test_the_wait_is_bounded_by_the_timeout_not_the_poll():
    """A poll interval longer than the time left must not overshoot — the
    last sleep is clipped to the deadline, so a 50 ms budget cannot become a
    one-second stall behind a slow poll setting."""
    bus = Bus(attaches_after=None)
    started = asyncio.get_running_loop().time()
    assert await wait_for_reader(bus, "c", timeout_s=0.05, poll_s=5.0) is False
    assert asyncio.get_running_loop().time() - started < 1.0


async def test_a_zero_timeout_asks_once_and_does_not_sleep():
    """The degenerate case still has to ask: a reader that is ALREADY there
    must be found without waiting at all."""
    bus = Bus(attaches_after=0)
    assert await wait_for_reader(bus, "c", timeout_s=0.0) is True
    assert bus.polls == 1

    absent = Bus(attaches_after=None)
    assert await wait_for_reader(absent, "c", timeout_s=0.0) is False
    assert absent.polls == 1
