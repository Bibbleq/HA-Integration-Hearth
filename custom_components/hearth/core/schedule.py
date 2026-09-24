"""Warm-by schedule model (spec 4.5).

A schedule is a dict of weekday -> list of blocks. Blocks are either
`warm_by` (the room should *be* at the preset by that time, so preheat applies)
or `at` (switch at that time). Times are local wall-clock "HH:MM".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any

WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
NON_WORKDAY = "non_workday"  # optional blocks for a Mon-Fri date that is not a workday (bank holiday)
DAY_KEYS = (*WEEKDAYS, NON_WORKDAY)
WEEKDAY_ALIASES = {
    "monday": "mon",
    "tuesday": "tue",
    "wednesday": "wed",
    "thursday": "thu",
    "friday": "fri",
    "holiday": NON_WORKDAY,
    "non-workday": NON_WORKDAY,
    "saturday": "sat",
    "sunday": "sun",
}
VALID_PRESETS = ("frost", "eco", "comfort", "boost")


class ScheduleError(ValueError):
    """Raised for an invalid schedule blob."""


def parse_time(value: str) -> time:
    try:
        hh, mm = value.strip().split(":")
        return time(int(hh), int(mm))
    except (ValueError, AttributeError) as err:
        raise ScheduleError(f"Invalid time {value!r}, expected HH:MM") from err


@dataclass(frozen=True)
class Block:
    """One schedule block."""

    at: time
    preset: str
    warm_by: bool  # True for warm_by blocks (preheat applies)
    skippable: bool = False

    def to_dict(self) -> dict:
        key = "warm_by" if self.warm_by else "at"
        data: dict[str, Any] = {key: self.at.strftime("%H:%M"), "preset": self.preset}
        if self.skippable:
            data["skippable"] = True
        return data


@dataclass(frozen=True)
class BlockInstance:
    """A block bound to a concrete day. `start` is the aware datetime of the block time."""

    block: Block
    start: datetime

    @property
    def key(self) -> str:
        return f"{self.start.isoformat()}|{self.block.preset}"

    @property
    def preset(self) -> str:
        return self.block.preset


def parse_block(data: dict) -> Block:
    if not isinstance(data, dict):
        raise ScheduleError(f"Block must be a mapping, got {data!r}")
    warm_by = data.get("warm_by")
    at = data.get("at")
    if (warm_by is None) == (at is None):
        raise ScheduleError(f"Block needs exactly one of 'warm_by' or 'at': {data!r}")
    preset = str(data.get("preset", "")).lower()
    if preset not in VALID_PRESETS:
        raise ScheduleError(f"Block preset must be one of {VALID_PRESETS}: {data!r}")
    skippable = bool(data.get("skippable", False))
    return Block(parse_time(str(warm_by if warm_by is not None else at)), preset, warm_by is not None, skippable)


class Schedule:
    """Per-room weekly schedule.

    A Mon-Fri date listed in `non_workdays` uses the `non_workday` blocks when the
    schedule defines them, otherwise Saturday's blocks. The glue fills
    `non_workdays` from a workday sensor; with no sensor every date follows its weekday.
    """

    def __init__(self, days: dict[str, list[Block]]) -> None:
        self.days = {d: sorted(days.get(d, []), key=lambda b: b.at) for d in DAY_KEYS}
        self.non_workdays: set[date] = set()

    @classmethod
    def parse(cls, blob: dict | None) -> Schedule:
        if blob is None:
            return cls({})
        if not isinstance(blob, dict):
            raise ScheduleError("Schedule must be a mapping of weekday to block list")
        days: dict[str, list[Block]] = {}
        for raw_day, blocks in blob.items():
            day = WEEKDAY_ALIASES.get(str(raw_day).lower(), str(raw_day).lower())
            if day not in DAY_KEYS:
                raise ScheduleError(f"Unknown weekday {raw_day!r}")
            if blocks is None:
                blocks = []
            if not isinstance(blocks, list):
                raise ScheduleError(f"Blocks for {day} must be a list")
            parsed = [parse_block(b) for b in blocks]
            times = [b.at for b in parsed]
            if len(set(times)) != len(times):
                raise ScheduleError(f"Duplicate block times on {day}")
            days[day] = parsed
        return cls(days)

    def to_dict(self) -> dict:
        return {d: [b.to_dict() for b in self.days[d]] for d in DAY_KEYS if self.days[d]}

    @property
    def is_empty(self) -> bool:
        return not any(self.days[d] for d in WEEKDAYS)

    def day_key(self, day: date) -> str:
        """Which block list applies on `day`."""
        if day.weekday() < 5 and day in self.non_workdays:
            return NON_WORKDAY if self.days[NON_WORKDAY] else "sat"
        return WEEKDAYS[day.weekday()]

    def blocks_on(self, day: date) -> list[Block]:
        return self.days[self.day_key(day)]

    def instances_between(self, start: datetime, end: datetime, tzinfo) -> list[BlockInstance]:
        """All block instances with start <= block time < end, ordered."""
        out: list[BlockInstance] = []
        day = (start - timedelta(days=1)).date()
        last_day = end.date() + timedelta(days=1)
        while day <= last_day:
            for block in self.blocks_on(day):
                at = datetime.combine(day, block.at, tzinfo=tzinfo)
                if start <= at < end:
                    out.append(BlockInstance(block, at))
            day += timedelta(days=1)
        return out

    def current(self, now: datetime, tzinfo, lookback_days: int = 8) -> BlockInstance | None:
        """The block in force at `now`: the latest instance whose time is <= now."""
        start = now - timedelta(days=lookback_days)
        inst = self.instances_between(start, now + timedelta(seconds=1), tzinfo)
        return inst[-1] if inst else None

    def next(self, now: datetime, tzinfo, lookahead_days: int = 8) -> BlockInstance | None:
        """The first block instance strictly after `now`."""
        inst = self.instances_between(now + timedelta(seconds=1), now + timedelta(days=lookahead_days), tzinfo)
        return inst[0] if inst else None

    def next_boundary(self, now: datetime, tzinfo) -> datetime | None:
        nxt = self.next(now, tzinfo)
        return nxt.start if nxt else None

    def next_warm_by(self, now: datetime, tzinfo, lookahead_days: int = 8) -> BlockInstance | None:
        """The first upcoming warm_by block."""
        for inst in self.instances_between(now + timedelta(seconds=1), now + timedelta(days=lookahead_days), tzinfo):
            if inst.block.warm_by:
                return inst
        return None

    def recent_warm_by(self, now: datetime, tzinfo, within: timedelta) -> BlockInstance | None:
        """The most recent warm_by block whose time is within `within` before now."""
        inst = [i for i in self.instances_between(now - within, now + timedelta(seconds=1), tzinfo) if i.block.warm_by]
        return inst[-1] if inst else None
