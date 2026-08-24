"""Opening-hours arithmetic — "is this restaurant taking orders right now?"

Lives in smartfood-pricing beside the engine because it answers the same
question the engine already asks (`RestaurantClosed`), and it is the same
KIND of thing: a pure function of data handed to it. It does NOT read a
clock — the caller supplies `now`, which is what makes every edge case
below a plain unit test instead of a scheduled job.

The shape stored on the restaurant is `{"mon": ["11:00", "23:00"], ...}`:
a weekday key, and a flat list of "HH:MM" points read in PAIRS, so a split
day (lunch, then dinner) is just four entries:

    {"sat": ["11:00", "14:30", "17:00", "23:00"]}

Two decisions worth stating, because both are load-bearing and neither is
guessable from the data:

1. **Open is inclusive, close is exclusive** — `[11:00, 23:00)`. At exactly
   23:00 the kitchen is closed. This matches how humans read a posted
   "11-11" sign, and it makes back-to-back windows (["09:00","12:00",
   "12:00","15:00"]) continuous rather than double-counting noon.

2. **No hours means always open.** `hours=None` (and `{}`) preserves the
   behaviour every existing restaurant has today: only the owner's explicit
   pause closes the shop. Enforcing hours on restaurants that never set any
   would silently shut every seeded restaurant the moment this shipped.

Overnight windows (["18:00", "02:00"]) are the case that makes this more
than a string compare: the close time is BEFORE the open time, so the
window belongs partly to the following calendar day. We handle it by also
consulting YESTERDAY's schedule for a window that spilled past midnight —
so 01:00 Sunday is correctly served by Saturday's 18:00-02:00 shift.
"""

from datetime import datetime, time
from typing import Any, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# datetime.weekday(): Monday is 0. Keys are lowercase three-letter days,
# which is what the API accepts and what the seed writes.
_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

# A schedule we cannot parse must not silently close a restaurant that is
# genuinely open — the owner would lose orders and see no error. Validation
# happens at the API boundary (write time); here a bad value degrades OPEN,
# the same "loss degrades, never corrupts" contract the cache adapter uses.
_UNPARSEABLE_IS_OPEN = True


def _parse_hhmm(value: Any) -> time | None:
    """ "HH:MM" → time, or None for anything we cannot read."""
    if not isinstance(value, str):
        return None
    parts = value.split(":")
    if len(parts) != 2:
        return None
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return time(hour, minute)


def _windows(day_schedule: Any) -> list[tuple[time, time]] | None:
    """The day's [open, close) pairs, or None if the entry is unreadable."""
    if not isinstance(day_schedule, list):
        return None
    # Narrowing `Any` to a bare `list` leaves the element type UNKNOWN, which
    # strict pyright rejects. The values are untrusted JSON either way, so
    # say Any explicitly — _parse_hhmm is the thing that makes them safe.
    points = cast(list[Any], day_schedule)
    if len(points) % 2 != 0:  # an unpaired point has no meaning
        return None
    pairs: list[tuple[time, time]] = []
    for i in range(0, len(points), 2):
        opens, closes = _parse_hhmm(points[i]), _parse_hhmm(points[i + 1])
        if opens is None or closes is None:
            return None
        pairs.append((opens, closes))
    return pairs


def is_open_at(hours: dict[str, Any] | None, timezone: str, now: datetime) -> bool:
    """Is the restaurant inside a posted window at `now`?

    `now` is any aware datetime (UTC in production — it is converted into
    the restaurant's zone here, because a schedule is wall-clock local: a
    Chicago restaurant opens at 11am Chicago time in June and in January,
    even though that is two different UTC instants).
    """
    if not hours:  # None or {} — never configured, so never closed by schedule
        return True

    try:
        local = now.astimezone(ZoneInfo(timezone))
    except (ZoneInfoNotFoundError, ValueError):
        # An unknown zone is a data bug, not a customer's problem.
        return _UNPARSEABLE_IS_OPEN

    today = _windows(hours.get(_DAYS[local.weekday()]))
    if today is None and hours.get(_DAYS[local.weekday()]) is not None:
        return _UNPARSEABLE_IS_OPEN
    for opens, closes in today or ():
        if opens <= closes:  # ordinary same-day window
            if opens <= local.time() < closes:
                return True
        elif local.time() >= opens:  # overnight, and we are in its first half
            return True

    # Yesterday's overnight window may still be running (01:00 served by the
    # previous day's 18:00-02:00). Only the SECOND half can reach today, so
    # this checks `local.time() < closes` and nothing else.
    yesterday = _windows(hours.get(_DAYS[(local.weekday() - 1) % 7]))
    for opens, closes in yesterday or ():
        if closes < opens and local.time() < closes:
            return True

    return False
