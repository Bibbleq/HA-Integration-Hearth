"""Tests for core.schedule (spec 4.5)."""

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.hearth.core.schedule import Block, Schedule, ScheduleError, parse_block, parse_time

LONDON = ZoneInfo("Europe/London")

WEEKDAY = [
    {"warm_by": "06:30", "preset": "comfort", "skippable": True},
    {"at": "09:00", "preset": "eco"},
    {"warm_by": "17:00", "preset": "comfort"},
    {"at": "22:30", "preset": "eco"},
]
BLOB = {d: WEEKDAY for d in ("mon", "tue", "wed", "thu", "fri")} | {
    "sat": [{"warm_by": "08:30", "preset": "comfort"}, {"at": "22:00", "preset": "eco"}],
    "sun": [{"warm_by": "08:30", "preset": "comfort"}, {"at": "22:00", "preset": "eco"}],
}


def test_parse_time():
    assert parse_time("06:30") == time(6, 30)
    assert parse_time(" 7:05 ") == time(7, 5)
    with pytest.raises(ScheduleError):
        parse_time("6h30")
    with pytest.raises(ScheduleError):
        parse_time("25:00")


def test_parse_block_validation():
    b = parse_block({"warm_by": "06:30", "preset": "Comfort", "skippable": True})
    assert b == Block(time(6, 30), "comfort", True, True)
    assert parse_block({"at": "22:00", "preset": "eco"}).warm_by is False
    with pytest.raises(ScheduleError):
        parse_block({"preset": "eco"})
    with pytest.raises(ScheduleError):
        parse_block({"at": "1:00", "warm_by": "2:00", "preset": "eco"})
    with pytest.raises(ScheduleError):
        parse_block({"at": "1:00", "preset": "sleep"})
    with pytest.raises(ScheduleError):
        parse_block("nope")


def test_parse_schedule_and_roundtrip():
    s = Schedule.parse(BLOB)
    assert not s.is_empty
    assert [b.at for b in s.days["mon"]] == [time(6, 30), time(9, 0), time(17, 0), time(22, 30)]
    assert Schedule.parse(s.to_dict()).to_dict() == s.to_dict()
    assert Schedule.parse(None).is_empty
    assert Schedule.parse({"Monday": None}).is_empty


def test_parse_schedule_errors():
    with pytest.raises(ScheduleError):
        Schedule.parse(["not", "a", "dict"])
    with pytest.raises(ScheduleError):
        Schedule.parse({"funday": []})
    with pytest.raises(ScheduleError):
        Schedule.parse({"mon": {"at": "1:00"}})
    with pytest.raises(ScheduleError):
        Schedule.parse({"mon": [{"at": "06:00", "preset": "eco"}, {"warm_by": "06:00", "preset": "comfort"}]})


def test_sorting_out_of_order_blocks():
    s = Schedule.parse({"mon": [{"at": "22:00", "preset": "eco"}, {"at": "06:00", "preset": "comfort"}]})
    assert [b.at for b in s.days["mon"]] == [time(6, 0), time(22, 0)]


def test_current_and_next_block():
    s = Schedule.parse(BLOB)
    # Wednesday 2026-01-14 10:00 local
    now = datetime(2026, 1, 14, 10, 0, tzinfo=LONDON)
    cur = s.current(now, LONDON)
    assert cur.preset == "eco" and cur.start == datetime(2026, 1, 14, 9, 0, tzinfo=LONDON)
    nxt = s.next(now, LONDON)
    assert nxt.preset == "comfort" and nxt.start == datetime(2026, 1, 14, 17, 0, tzinfo=LONDON)
    assert s.next_boundary(now, LONDON) == nxt.start


def test_current_block_crosses_midnight():
    s = Schedule.parse(BLOB)
    now = datetime(2026, 1, 15, 2, 0, tzinfo=LONDON)  # Thursday 02:00
    cur = s.current(now, LONDON)
    assert cur.preset == "eco" and cur.start == datetime(2026, 1, 14, 22, 30, tzinfo=LONDON)


def test_current_block_at_exact_time_is_inclusive():
    s = Schedule.parse(BLOB)
    now = datetime(2026, 1, 14, 6, 30, tzinfo=LONDON)
    assert s.current(now, LONDON).start == now
    assert s.next(now, LONDON).start == datetime(2026, 1, 14, 9, 0, tzinfo=LONDON)


def test_next_warm_by_and_recent_warm_by():
    s = Schedule.parse(BLOB)
    now = datetime(2026, 1, 14, 10, 0, tzinfo=LONDON)
    assert s.next_warm_by(now, LONDON).start == datetime(2026, 1, 14, 17, 0, tzinfo=LONDON)
    assert s.recent_warm_by(now, LONDON, timedelta(hours=1)) is None
    now = datetime(2026, 1, 14, 7, 15, tzinfo=LONDON)
    assert s.recent_warm_by(now, LONDON, timedelta(hours=1)).start == datetime(2026, 1, 14, 6, 30, tzinfo=LONDON)


def test_sparse_schedule_looks_back_across_days():
    s = Schedule.parse({"mon": [{"at": "07:00", "preset": "comfort"}]})
    now = datetime(2026, 1, 15, 12, 0, tzinfo=LONDON)  # Thursday
    cur = s.current(now, LONDON)
    assert cur.start == datetime(2026, 1, 12, 7, 0, tzinfo=LONDON)
    nxt = s.next(now, LONDON)
    assert nxt.start == datetime(2026, 1, 19, 7, 0, tzinfo=LONDON)


def test_empty_schedule_has_no_blocks():
    s = Schedule.parse({})
    now = datetime(2026, 1, 15, 12, 0, tzinfo=LONDON)
    assert s.current(now, LONDON) is None
    assert s.next(now, LONDON) is None
    assert s.next_boundary(now, LONDON) is None


def test_dst_transition_keeps_wall_clock():
    """Blocks are wall-clock times; the UTC offset changes across the DST switch."""
    s = Schedule.parse({"sun": [{"warm_by": "06:30", "preset": "comfort"}]})
    # 2026-03-29 is the UK spring-forward Sunday
    now = datetime(2026, 3, 29, 5, 0, tzinfo=LONDON)
    nxt = s.next(now, LONDON)
    assert nxt.start.hour == 6 and nxt.start.minute == 30
    assert nxt.start.utcoffset() == timedelta(hours=1)
    prev = datetime(2026, 3, 22, 6, 30, tzinfo=LONDON)
    assert prev.utcoffset() == timedelta(0)


def test_block_instance_key_unique_per_day():
    s = Schedule.parse(BLOB)
    a = s.current(datetime(2026, 1, 14, 10, 0, tzinfo=LONDON), LONDON)
    b = s.current(datetime(2026, 1, 15, 10, 0, tzinfo=LONDON), LONDON)
    assert a.key != b.key and a.preset == b.preset


def test_non_workday_uses_saturday_blocks_by_default():
    from datetime import date

    s = Schedule.parse(BLOB)
    bank_holiday = date(2026, 8, 31)  # a Monday
    s.non_workdays = {bank_holiday}
    assert s.day_key(bank_holiday) == "sat"
    assert [b.at for b in s.blocks_on(bank_holiday)] == [time(8, 30), time(22, 0)]
    now = datetime(2026, 8, 31, 7, 0, tzinfo=LONDON)
    assert s.next(now, LONDON).start == datetime(2026, 8, 31, 8, 30, tzinfo=LONDON)
    # The next day is an ordinary Tuesday again
    assert s.day_key(date(2026, 9, 1)) == "tue"


def test_non_workday_blocks_when_defined_and_weekends_unaffected():
    from datetime import date

    blob = dict(BLOB)
    blob["holiday"] = [{"warm_by": "09:00", "preset": "comfort"}]
    s = Schedule.parse(blob)
    assert "non_workday" in s.to_dict()
    s.non_workdays = {date(2026, 8, 31), date(2026, 9, 5)}  # Monday, Saturday
    assert s.day_key(date(2026, 8, 31)) == "non_workday"
    assert s.day_key(date(2026, 9, 5)) == "sat"  # weekends always use their own blocks
    assert Schedule.parse(s.to_dict()).to_dict() == s.to_dict()


def test_non_workday_only_schedule_counts_as_empty():
    assert Schedule.parse({"non_workday": [{"at": "07:00", "preset": "eco"}]}).is_empty
