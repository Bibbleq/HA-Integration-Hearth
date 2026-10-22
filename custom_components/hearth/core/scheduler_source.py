"""Derive a Hearth schedule from HA Scheduler component entities (D31-D36).

Pure logic, no Home Assistant imports. The glue reads each `switch.schedule_*` entity's
state attributes and hands them in as plain data:

    weekdays   list of mon..sun, or the shorthands daily / workday / weekend
    timeslots  "HH:MM:SS - HH:MM:SS", or "HH:MM:SS" when a slot has no stop
    actions    list aligned 1:1 with timeslots, {"service": ..., "data": {...}}

Each timeslot start becomes one Hearth block. The slot's stop time is ignored: Hearth
works from block starts, and the next block ends the previous one.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import time
from typing import Any

from .schedule import NON_WORKDAY, VALID_PRESETS, WEEKDAYS, Block, Schedule

# Repair keys. The translation keys in strings.json and the issue ids use the same strings.
ISSUE_DAY_CONFLICT = "scheduler_day_conflict"
ISSUE_SHARED_SOURCE = "scheduler_shared_source"
ISSUE_UNSUPPORTED_ACTION = "scheduler_unsupported_action"
ISSUE_SOURCE_ENABLED = "scheduler_source_enabled"

SERVICE_SET_PRESET = "climate.set_preset_mode"
WEEKEND = ("sat", "sun")
WORKDAYS = ("mon", "tue", "wed", "thu", "fri")
_FULL_NAMES = {
    "monday": "mon",
    "tuesday": "tue",
    "wednesday": "wed",
    "thursday": "thu",
    "friday": "fri",
    "saturday": "sat",
    "sunday": "sun",
}
_CLOCK = re.compile(r"^\s*(\d{1,2}):(\d{2})(?::(\d{2}))?\s*$")


class SourceError(ValueError):
    """A Scheduler entity that Hearth cannot follow (becomes `scheduler_unsupported_action`)."""


@dataclass(frozen=True)
class SourceInput:
    """The attributes of one Scheduler entity, as read from its state."""

    entity_id: str
    weekdays: Any
    timeslots: Any
    actions: Any


@dataclass(frozen=True)
class Issue:
    """One problem found while deriving. `key` is the repair key."""

    key: str
    entity_ids: tuple[str, ...]
    detail: str = ""


@dataclass
class Derivation:
    """The result of deriving a schedule from a set of Scheduler entities."""

    schedule: Schedule
    issues: list[Issue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when the schedule is complete and may be applied."""
        return not self.issues

    def issue(self, key: str) -> Issue | None:
        return next((i for i in self.issues if i.key == key), None)


def expand_weekdays(weekdays: Any) -> set[str]:
    """Expand a Scheduler `weekdays` attribute to a set of mon..sun."""
    if not isinstance(weekdays, (list, tuple)) or not weekdays:
        raise SourceError("no weekdays")
    out: set[str] = set()
    for raw in weekdays:
        item = str(raw).strip().lower()
        item = _FULL_NAMES.get(item, item)
        if item == "daily":
            out.update(WEEKDAYS)
        elif item == "workday":
            out.update(WORKDAYS)
        elif item == "weekend":
            out.update(WEEKEND)
        elif item in WEEKDAYS:
            out.add(item)
        else:
            raise SourceError(f"weekday {raw!r} not understood")
    return out


def slot_start(slot: Any) -> time:
    """The start time of a timeslot string: "HH:MM:SS - HH:MM:SS" or "HH:MM:SS"."""
    if not isinstance(slot, str):
        raise SourceError(f"timeslot {slot!r} is not text")
    start = slot.split(" - ")[0]
    match = _CLOCK.match(start)
    if match is None:
        raise SourceError(f"timeslot {slot!r} does not start at a fixed time (sunrise and sunset slots are not supported)")
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        raise SourceError(f"timeslot {slot!r} has an invalid start time")
    return time(hour, minute)


def action_preset(action: Any) -> str:
    """The preset a slot action sets. Only `climate.set_preset_mode` to a Hearth preset is supported."""
    if not isinstance(action, dict):
        raise SourceError(f"action {action!r} is not a service call")
    service = str(action.get("service", "")).strip().lower()
    if service != SERVICE_SET_PRESET:
        raise SourceError(f"action {service or action!r} is not {SERVICE_SET_PRESET}")
    data = action.get("data")
    if not isinstance(data, dict):
        data = action.get("service_data")
    preset = str((data or {}).get("preset_mode", "")).strip().lower()
    if preset not in VALID_PRESETS:
        raise SourceError(f"preset {preset or '(none)'!r} is not one of {', '.join(VALID_PRESETS)}")
    return preset


def entity_blocks(source: SourceInput, warm_by_presets: Iterable[str]) -> list[Block]:
    """One block per timeslot, sorted by time. Raises SourceError when the entity is unusable."""
    slots, actions = source.timeslots, source.actions
    if not isinstance(slots, (list, tuple)) or not isinstance(actions, (list, tuple)):
        raise SourceError("no timeslots or actions")
    if not slots:
        raise SourceError("no timeslots")
    if len(slots) != len(actions):
        raise SourceError(f"{len(slots)} timeslots but {len(actions)} actions")
    warm = set(warm_by_presets)
    blocks: list[Block] = []
    for slot, action in zip(slots, actions, strict=True):
        at = slot_start(slot)
        preset = action_preset(action)
        blocks.append(Block(at, preset, preset in warm))
    blocks.sort(key=lambda b: b.at)
    times = [b.at for b in blocks]
    if len(set(times)) != len(times):
        raise SourceError("two timeslots start at the same time")
    return blocks


def mark_skippable(blocks: Sequence[Block], cutoff: time) -> list[Block]:
    """Mark the first warm-by block of the day skippable when it falls before `cutoff`."""
    out = list(blocks)
    for i, block in enumerate(out):
        if block.warm_by:
            if block.at < cutoff:
                out[i] = replace(block, skippable=True)
            break
    return out


def derive_schedule(
    sources: Sequence[SourceInput],
    *,
    warm_by_presets: Iterable[str],
    skippable_first_morning: bool = True,
    morning_cutoff: time = time(12, 0),
) -> Derivation:
    """Turn a set of Scheduler entities into a Hearth Schedule plus any issues found.

    Issues block the result (`Derivation.ok` is False): an entity whose slots Hearth cannot
    follow, or two entities claiming the same day. A day no entity covers has no blocks, so the
    previous day's last preset carries over. An entity covering exactly Saturday and Sunday also
    supplies the `non_workday` blocks.
    """
    issues: list[Issue] = []
    claims: dict[str, list[str]] = {}
    per_entity: list[tuple[str, set[str], list[Block]]] = []
    warm_set = set(warm_by_presets)
    for source in sources:
        try:
            covered = expand_weekdays(source.weekdays)
            blocks = entity_blocks(source, warm_set)
        except SourceError as err:
            issues.append(Issue(ISSUE_UNSUPPORTED_ACTION, (source.entity_id,), str(err)))
            continue
        if skippable_first_morning:
            blocks = mark_skippable(blocks, morning_cutoff)
        per_entity.append((source.entity_id, covered, blocks))
        for day in covered:
            claims.setdefault(day, []).append(source.entity_id)

    conflicted = {day for day, owners in claims.items() if len(owners) > 1}
    if conflicted:
        involved = sorted({eid for day in conflicted for eid in claims[day]})
        days = ", ".join(d for d in WEEKDAYS if d in conflicted)
        issues.append(Issue(ISSUE_DAY_CONFLICT, tuple(involved), f"days: {days}"))

    days_out: dict[str, list[Block]] = {}
    for _entity_id, covered, blocks in per_entity:
        for day in covered - conflicted:
            days_out[day] = list(blocks)
        if covered == set(WEEKEND) and not (covered & conflicted):
            days_out[NON_WORKDAY] = list(blocks)
    return Derivation(Schedule(days_out), issues)
