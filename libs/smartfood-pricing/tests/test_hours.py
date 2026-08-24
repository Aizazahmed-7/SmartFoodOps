"""Opening-hours arithmetic — every branch, and the cases that make this
more than a string compare: overnight shifts, split days, DST, bad data."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from smartfood_pricing.hours import is_open_at

CHI = "America/Chicago"


def at(y, m, d, hh, mm=0, tz=CHI):
    """A wall-clock moment IN the restaurant's zone, handed to is_open_at as
    the aware UTC datetime production would pass."""
    return datetime(y, m, d, hh, mm, tzinfo=ZoneInfo(tz)).astimezone(UTC)


# 2026-08-24 is a Monday; 2026-08-29 a Saturday, 2026-08-30 a Sunday.
MON_11_TO_23 = {"mon": ["11:00", "23:00"]}


def test_no_hours_means_always_open():
    """Every seeded restaurant has hours=None. Enforcing a schedule nobody
    configured would have shut the whole catalog the moment this shipped."""
    assert is_open_at(None, CHI, at(2026, 8, 24, 4)) is True
    assert is_open_at({}, CHI, at(2026, 8, 24, 4)) is True


def test_inside_the_window():
    assert is_open_at(MON_11_TO_23, CHI, at(2026, 8, 24, 12)) is True


def test_before_and_after_are_closed():
    assert is_open_at(MON_11_TO_23, CHI, at(2026, 8, 24, 10, 59)) is False
    assert is_open_at(MON_11_TO_23, CHI, at(2026, 8, 24, 23, 1)) is False


def test_boundaries_open_inclusive_close_exclusive():
    """[11:00, 23:00) — at exactly closing time the kitchen is shut. This is
    what makes back-to-back windows continuous instead of overlapping."""
    assert is_open_at(MON_11_TO_23, CHI, at(2026, 8, 24, 11, 0)) is True
    assert is_open_at(MON_11_TO_23, CHI, at(2026, 8, 24, 23, 0)) is False


def test_a_day_with_no_entry_is_closed():
    """Monday-only hours mean Tuesday is shut — absence of a key is a real
    answer, not missing data."""
    assert is_open_at(MON_11_TO_23, CHI, at(2026, 8, 25, 12)) is False


def test_split_day_lunch_then_dinner():
    """Four points = two windows. The gap between them is closed."""
    hours = {"sat": ["11:00", "14:30", "17:00", "23:00"]}
    assert is_open_at(hours, CHI, at(2026, 8, 29, 12)) is True  # lunch
    assert is_open_at(hours, CHI, at(2026, 8, 29, 15, 30)) is False  # the gap
    assert is_open_at(hours, CHI, at(2026, 8, 29, 18)) is True  # dinner


def test_back_to_back_windows_are_continuous():
    """09:00-12:00 then 12:00-15:00: noon must be open exactly once, not
    fall down the crack between an exclusive close and an inclusive open."""
    hours = {"sat": ["09:00", "12:00", "12:00", "15:00"]}
    assert is_open_at(hours, CHI, at(2026, 8, 29, 12, 0)) is True


def test_overnight_window_before_midnight():
    hours = {"sat": ["18:00", "02:00"]}
    assert is_open_at(hours, CHI, at(2026, 8, 29, 23)) is True


def test_overnight_window_after_midnight_is_served_by_yesterday():
    """01:00 Sunday belongs to SATURDAY's 18:00-02:00 shift. Reading only
    today's key would wrongly close the restaurant at midnight."""
    hours = {"sat": ["18:00", "02:00"]}
    assert is_open_at(hours, CHI, at(2026, 8, 30, 1)) is True
    assert is_open_at(hours, CHI, at(2026, 8, 30, 2, 0)) is False  # close is exclusive
    assert is_open_at(hours, CHI, at(2026, 8, 30, 3)) is False


def test_overnight_wraps_from_sunday_to_monday():
    """The weekday index must wrap: Monday 01:00 is served by SUNDAY."""
    hours = {"sun": ["20:00", "03:00"]}
    assert is_open_at(hours, CHI, at(2026, 8, 31, 1)) is True


def test_yesterdays_ordinary_window_does_not_leak_into_today():
    """A same-day window that ended last night must NOT keep today open —
    only a genuinely overnight window reaches across midnight."""
    hours = {"sat": ["11:00", "23:00"]}
    assert is_open_at(hours, CHI, at(2026, 8, 30, 1)) is False


def test_timezone_is_the_restaurants_not_the_servers():
    """The same UTC instant is lunchtime in Chicago and late night in Tokyo.
    A schedule is wall-clock local or it is meaningless."""
    noon_chicago = at(2026, 8, 24, 12)
    assert is_open_at(MON_11_TO_23, CHI, noon_chicago) is True
    assert is_open_at(MON_11_TO_23, "Asia/Tokyo", noon_chicago) is False


def test_dst_spring_forward_day():
    """2026-03-08 is US spring-forward: 02:00 never happens in Chicago.
    zoneinfo handles the jump; what matters is that a normal 11-23 shift on
    that date behaves normally rather than shifting by an hour."""
    hours = {"sun": ["11:00", "23:00"]}
    assert is_open_at(hours, CHI, at(2026, 3, 8, 10, 59)) is False
    assert is_open_at(hours, CHI, at(2026, 3, 8, 11, 30)) is True
    assert is_open_at(hours, CHI, at(2026, 3, 8, 22, 59)) is True


def test_dst_fall_back_repeated_hour():
    """2026-11-01, US fall-back: 01:30 happens twice. Both instants are
    inside an 11-23 shift's day and must not flip the answer."""
    hours = {"sun": ["01:00", "23:00"]}
    assert is_open_at(hours, CHI, at(2026, 11, 1, 1, 30)) is True


def test_unknown_timezone_degrades_open():
    """A bad zone is a data bug. Closing the shop over it would cost the
    owner real orders with no error anyone would see."""
    assert is_open_at(MON_11_TO_23, "Mars/Olympus_Mons", at(2026, 8, 24, 4)) is True
    assert is_open_at(MON_11_TO_23, "not a zone at all", at(2026, 8, 24, 4)) is True


def test_malformed_schedules_degrade_open():
    """Same reasoning, one layer down: unreadable hours must not silently
    close a restaurant. The API validates at write time; this is the net."""
    bad = [
        {"mon": ["11:00"]},  # unpaired point
        {"mon": "11:00-23:00"},  # not a list
        {"mon": ["11h00", "23:00"]},  # unparseable time
        {"mon": ["25:00", "26:00"]},  # out of range
        {"mon": ["11:00", None]},  # wrong type inside the pair
        {"mon": [1100, 2300]},  # ints, not strings
    ]
    for hours in bad:
        assert is_open_at(hours, CHI, at(2026, 8, 24, 4)) is True, hours


def test_a_bad_day_entry_does_not_poison_other_days():
    """Tuesday being garbage must not decide Monday's answer."""
    hours = {"mon": ["11:00", "23:00"], "tue": ["oops"]}
    assert is_open_at(hours, CHI, at(2026, 8, 24, 4)) is False  # Monday, before open
    assert is_open_at(hours, CHI, at(2026, 8, 25, 4)) is True  # Tuesday, degraded open


def test_overnight_window_before_its_own_open_time():
    """Saturday 10:00 against a Saturday 18:00-02:00 shift: the window is
    overnight but has not STARTED yet, so the day's loop must fall through
    to the yesterday check rather than short-circuiting open."""
    hours = {"sat": ["18:00", "02:00"]}
    assert is_open_at(hours, CHI, at(2026, 8, 29, 10)) is False


def test_non_numeric_time_parts_degrade_open():
    """Has the colon, so it parses as two parts — but neither is a number.
    A different failure path from '11h00', which never splits at all."""
    assert is_open_at({"mon": ["ab:cd", "23:00"]}, CHI, at(2026, 8, 24, 12)) is True
