"""Scheduler entities as a room's schedule source, with take-over and fallback (D31-D37).

When `scheduler_entity_ids` is set, the room's schedule is derived from those HA Scheduler
(`switch.schedule_*`) entities instead of the stored blob. Hearth owns preset switching only
while the room's schedule switch is on and the global switch is on; it then switches the source
schedules off, and switches them back on whenever it stops owning the room (schedule switch off,
global switch off, unload, removal). Dormancy (including the global pause) and stand-down are temporary and never hand back.
A source that is switched back on while Hearth owns the room is switched off again (D48).
"""

from __future__ import annotations

import logging
from datetime import datetime, time, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import EventStateChangedData, async_track_point_in_utc_time, async_track_state_change_event
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from .const import (
    CONF_SCHEDULER_ENTITIES,
    CONF_SKIPPABLE_FIRST_MORNING,
    CONF_VTHERM,
    CONF_WARM_BY_PRESETS,
    DOMAIN,
    ISSUE_DAY_CONFLICT,
    ISSUE_SHARED_SOURCE,
    ISSUE_SOURCE_ENABLED,
    ISSUE_UNSUPPORTED_ACTION,
    SWITCH_SCHEDULE,
)
from .core.schedule import Schedule, ScheduleError, parse_time
from .core.scheduler_source import Issue, SourceInput, derive_schedule

_LOGGER = logging.getLogger(__name__)

ALL_ISSUES = (ISSUE_DAY_CONFLICT, ISSUE_SHARED_SOURCE, ISSUE_UNSUPPORTED_ACTION, ISSUE_SOURCE_ENABLED)
# What "blocked" means for sensor.<room>_next_block, most important first. source_enabled never blocks.
BLOCKING_PRIORITY = (ISSUE_SHARED_SOURCE, ISSUE_UNSUPPORTED_ACTION, ISSUE_DAY_CONFLICT)
_PENDING_STATES = (ConfigEntryState.NOT_LOADED, ConfigEntryState.SETUP_IN_PROGRESS, ConfigEntryState.UNLOAD_IN_PROGRESS)

_REASSERT_RETRY_S = 5  # seconds after a source came back on before a source still on is switched off again

SHARED_OK = "ok"
SHARED_PENDING = "pending"  # a Hearth room that has not finished loading: wait, no repair
SHARED_BLOCKED = "blocked"


def _issue_id(key: str, entry_id: str) -> str:
    return f"{key}_{entry_id}"


def _dt(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = dt_util.parse_datetime(value)
    if parsed is not None and parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt_util.UTC)
    return parsed


class SchedulerSourceMixin:
    """Derive the schedule from Scheduler entities and manage the hand-over. Mixed into HearthRoom."""

    _src_good: dict[str, Any] | None  # last good derivation: {"sources": [...], "schedule": blob}
    _src_issues: list[Issue]
    _src_missing: tuple[str, ...]
    _src_unsub: CALLBACK_TYPE | None
    _src_applied: dict | None
    _took_over: bool
    _took_over_at: datetime | None
    _enabled_timer: CALLBACK_TYPE | None
    _source_reenabled_at: datetime | None
    _reasserting: bool
    _issue_state: dict[str, dict[str, str]]
    _shared_state: str
    _shared_offenders: list[str]
    _not_targeted_warned: set[str]
    derived_at: datetime | None
    settings_loaded: bool
    ready: bool

    def _init_source(self) -> None:
        self._src_good = None
        self._src_issues = []
        self._src_missing = ()
        self._src_unsub = None
        self._src_applied = None
        self._took_over = False
        self._took_over_at = None
        self._enabled_timer = None
        self._source_reenabled_at = None
        self._reasserting = False
        self._issue_state = {}
        self._shared_state = SHARED_OK
        self._shared_offenders = []
        self._not_targeted_warned = set()
        self.derived_at = None
        self.settings_loaded = False
        self.ready = False  # first evaluation done: safe for peers to re-evaluate

    # ------------------------------------------------------------- config access

    @property
    def source_ids(self) -> list[str]:
        """The configured Scheduler entities, de-duplicated, in order."""
        raw = self.option(CONF_SCHEDULER_ENTITIES) or []
        if isinstance(raw, str):
            raw = [raw]
        return list(dict.fromkeys(str(e) for e in raw if e))

    @property
    def schedule_source(self) -> str:
        if self.source_ids:
            return "scheduler"
        return "explicit" if self.option("schedule") else "none"

    @property
    def source_blocked_reason(self) -> str | None:
        active = {i.key for i in self._src_issues}
        if self._shared_state == SHARED_BLOCKED:
            active.add(ISSUE_SHARED_SOURCE)
        for key in BLOCKING_PRIORITY:
            if key in active:
                return key
        return None

    @property
    def took_over(self) -> bool:
        return self._took_over

    # ------------------------------------------------------------- persistence

    def _load_source_state(self) -> None:
        """Restore the last good derivation (so a restart without Scheduler up still has a schedule)."""
        ids = self.source_ids
        if not ids:
            return
        saved = self.mechanisms.get("scheduler_source") or {}
        if saved.get("sources") != ids or not isinstance(saved.get("schedule"), dict):
            self.schedule_model = Schedule({})
            return
        try:
            self.schedule_model = Schedule.parse(saved["schedule"])
        except ScheduleError:
            self.schedule_model = Schedule({})
            return
        self._src_good = {"sources": ids, "schedule": saved["schedule"]}
        self.derived_at = _dt(saved.get("at"))

    def _dump_source_state(self) -> None:
        if self._src_good is not None and self.source_ids:
            self.mechanisms["scheduler_source"] = {
                "sources": self._src_good["sources"],
                "schedule": self._src_good["schedule"],
                "at": self.derived_at.isoformat() if self.derived_at else None,
            }
        else:
            self.mechanisms.pop("scheduler_source", None)

    # ------------------------------------------------------------- lifecycle

    def _subscribe_sources(self) -> None:
        ids = self.source_ids
        if not ids:
            return
        self._src_unsub = async_track_state_change_event(self.hass, ids, self._on_source_event)
        self._refresh_source("setup")

    async def async_source_unload(self) -> None:
        """Hand the sources back and clear this room's repairs."""
        self.ready = False
        if self._src_unsub is not None:
            self._src_unsub()
            self._src_unsub = None
        self._cancel_enabled_timer()
        if self._took_over:
            await self._hand_back("unload")
        for key in ALL_ISSUES:
            self._set_issue(key, None)

    @callback
    def _on_source_event(self, event: Event[EventStateChangedData]) -> None:
        self._refresh_source("source_event")
        new = event.data.get("new_state")
        old = event.data.get("old_state")
        if self._took_over and self.global_active and new is not None and new.state == STATE_ON and (old is None or old.state != STATE_ON):
            self._on_source_reenabled(dt_util.utcnow(), new.entity_id)
        self.hass.async_create_task(self.async_evaluate("scheduler_source"))

    def _on_source_reenabled(self, now: datetime, entity_id: str) -> None:
        """D48: a source Hearth switched off is on again (the Home Mode automation, a person, a Scheduler reload).

        The Scheduler fires its current slot the moment it is enabled. If that write reached the VTherm before this
        event, Hearth has already read it as a person's override: take that back. Then switch the source off and
        put Hearth's own block back.
        """
        self._source_reenabled_at = now
        undone = self.undo_recent_override(now)
        self._diagnostic(
            "scheduler_source_reenabled",
            f"{entity_id} was switched back on while Hearth owns the room; switching it off again"
            + (" (and ignoring the preset change it caused)" if undone else ""),
        )
        self._arm_enabled_timer(now)
        self._reasserting = True
        self.hass.async_create_task(self._async_reassert_sources())

    def _retry_reassert_due(self, now: datetime) -> bool:
        """A re-assert that did not stick is retried on later evaluations, not while the first attempt is settling."""
        at = self._source_reenabled_at
        return at is not None and not self._reasserting and (now - at).total_seconds() >= _REASSERT_RETRY_S

    def scheduler_grace_active(self, now: datetime) -> bool:
        """True while a preset change on the VTherm is explained by a Scheduler source that has just come on."""
        if not self._took_over:
            return False
        grace = timedelta(seconds=float(self.const("source_reenable_grace_s")))
        if self._source_reenabled_at is not None and now - self._source_reenabled_at <= grace:
            return True
        for entity_id in self.source_ids:  # the write can beat our own handling of the switch event
            state = self.hass.states.get(entity_id)
            if state is not None and state.state == STATE_ON and now - state.last_changed <= grace:
                return True
        return False

    def request_reapply(self) -> None:
        """Put Hearth's own current block back after the Scheduler wrote over it."""
        self.hass.async_create_task(self._async_reapply_own_block())

    async def _async_reapply_own_block(self) -> None:
        self.sched.pop("applied_key", None)
        await self.async_evaluate("scheduler_source_reapply")

    async def _async_reassert_sources(self) -> None:
        try:
            await self._turn_off_enabled_sources()
        finally:
            self._reasserting = False
        await self._async_reapply_own_block()

    async def _turn_off_enabled_sources(self) -> bool:
        """switch.turn_off on every source that is currently on. False if a call failed."""
        ok = True
        for entity_id in self.source_ids:
            state = self.hass.states.get(entity_id)
            if state is None or state.state != STATE_ON:
                continue
            try:
                await self.hass.services.async_call("switch", "turn_off", {"entity_id": entity_id}, blocking=True)
            except Exception as err:  # noqa: BLE001 - a failed Scheduler call must never take the controller down
                ok = False
                _LOGGER.warning("%s: switch.turn_off on %s failed: %s", self.room_name, entity_id, err)
        return ok

    async def _reconcile_peers(self) -> None:
        """Other rooms sharing a source depend on this room's schedule switch and load state."""
        for other in list(self.hass.data[DOMAIN]["rooms"].values()):
            if other is not self and other.ready and other.source_ids:
                await other.async_evaluate("peer_room")

    # ------------------------------------------------------------- derivation

    def _refresh_source(self, reason: str) -> None:
        """Read the source entities and re-derive. Keeps the last good schedule when it cannot."""
        ids = self.source_ids
        if not ids:
            return
        inputs: list[SourceInput] = []
        missing: list[str] = []
        for entity_id in ids:
            state = self.hass.states.get(entity_id)
            attrs = state.attributes if state is not None else {}
            if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN) or attrs.get("timeslots") is None:
                missing.append(entity_id)
                continue
            inputs.append(SourceInput(entity_id, attrs.get("weekdays"), attrs.get("timeslots"), attrs.get("actions")))
            self._warn_if_not_targeted(entity_id, attrs.get("entities"))
        if missing:
            if tuple(missing) != self._src_missing:
                self._src_missing = tuple(missing)
                kept = "keeping the last good schedule" if self._src_good else "no schedule derived yet"
                self._diagnostic(
                    "scheduler_source_missing",
                    f"Scheduler entities unavailable or not loaded: {', '.join(missing)}; {kept}",
                )
            return
        self._src_missing = ()
        derivation = derive_schedule(
            inputs,
            warm_by_presets=self.option(CONF_WARM_BY_PRESETS) or [],
            skippable_first_morning=bool(self.option(CONF_SKIPPABLE_FIRST_MORNING)),
            morning_cutoff=self._morning_cutoff(),
        )
        self._src_issues = list(derivation.issues)
        if not derivation.ok:
            _LOGGER.warning(
                "%s: Scheduler schedule not applied: %s", self.room_name, "; ".join(f"{i.key} {i.detail}" for i in derivation.issues)
            )
            return
        blob = derivation.schedule.to_dict()
        self.derived_at = dt_util.utcnow()
        if self._src_good is None or self._src_good["schedule"] != blob or self._src_good["sources"] != ids:
            self._src_good = {"sources": ids, "schedule": blob}
            blocks = sum(len(v) for v in blob.values())
            self._diagnostic(
                "scheduler_derived", f"Schedule derived from {', '.join(ids)}: {blocks} blocks over {len(blob)} day lists ({reason})"
            )

    def _morning_cutoff(self) -> time:
        try:
            return parse_time(str(self.const("morning_cutoff")))
        except Exception:  # noqa: BLE001 - a bad Tier 3 override falls back to the default
            return time(12, 0)

    def _warn_if_not_targeted(self, entity_id: str, targets: Any) -> None:
        if not isinstance(targets, list) or self.vtherm.entity_id in targets or entity_id in self._not_targeted_warned:
            return
        self._not_targeted_warned.add(entity_id)
        _LOGGER.warning(
            "%s: Scheduler entity %s does not list %s among its targets (%s); using it anyway",
            self.room_name,
            entity_id,
            self.vtherm.entity_id,
            ", ".join(map(str, targets)) or "none",
        )

    def _apply_source_schedule(self) -> None:
        """Make schedule_model the derived schedule, or empty while the shared-source guard blocks it."""
        blocked = self._shared_state != SHARED_OK
        blob = {} if blocked or self._src_good is None else self._src_good["schedule"]
        if self._src_applied == blob:
            return
        try:
            model = Schedule.parse(blob)
        except ScheduleError as err:  # cannot happen for a derived blob, but never take the controller down
            _LOGGER.error("%s: derived schedule rejected: %s", self.room_name, err)
            return
        model.non_workdays = set(self.schedule_model.non_workdays)
        self.schedule_model = model
        self._src_applied = dict(blob)
        self.preheat_plan = None

    # ------------------------------------------------------------- take-over and fallback

    async def _reconcile_source(self, now: datetime) -> None:
        """Called on every evaluation: apply the derived schedule, take over or hand back, sync repairs."""
        ids = self.source_ids
        if not ids:
            return
        wanted = self.enabled(SWITCH_SCHEDULE)
        self._shared_state, self._shared_offenders = self._shared_check() if wanted else (SHARED_OK, [])
        self._apply_source_schedule()
        can_take_over = wanted and self._shared_state == SHARED_OK and self._src_good is not None
        if can_take_over and not self._took_over:
            await self._take_over(now)
        elif not can_take_over and self._took_over:
            await self._hand_back("shared_source" if wanted else "schedule or global switch off")
        elif can_take_over and self._took_over and self._retry_reassert_due(now):
            await self._turn_off_enabled_sources()  # a re-assert that failed is retried here
        self._sync_issues(now)

    def _shared_check(self) -> tuple[str, list[str]]:
        """Every climate entity a source drives must be a Hearth room with its schedule switch on."""
        rooms = self.hass.data[DOMAIN]["rooms"]
        pending = False
        offenders: list[str] = []
        for entity_id in self.source_ids:
            state = self.hass.states.get(entity_id)
            targets = state.attributes.get("entities") if state is not None else None
            if not isinstance(targets, list):
                continue
            for target in dict.fromkeys(str(t) for t in targets):
                if not target.startswith("climate.") or target == self.vtherm.entity_id:
                    continue
                verdict = self._peer_verdict(rooms, target)
                if verdict == SHARED_PENDING:
                    pending = True
                elif verdict == SHARED_BLOCKED and target not in offenders:
                    offenders.append(target)
        if offenders:
            return SHARED_BLOCKED, offenders
        return (SHARED_PENDING if pending else SHARED_OK), []

    def _peer_verdict(self, rooms: dict, climate_id: str) -> str:
        for room in rooms.values():
            if room.vtherm.entity_id == climate_id:
                if not room.settings_loaded:
                    return SHARED_PENDING
                return SHARED_OK if room.enabled(SWITCH_SCHEDULE) else SHARED_BLOCKED
        for entry in self.hass.config_entries.async_entries(DOMAIN):
            if entry.data.get(CONF_VTHERM) == climate_id and entry.disabled_by is None and entry.state in _PENDING_STATES:
                return SHARED_PENDING
        return SHARED_BLOCKED

    async def _call_sources(self, service: str) -> bool:
        """switch.<service> on every source that exists. Returns False if a call failed (retried next evaluation)."""
        ok = True
        if not self.hass.services.has_service("switch", service):
            await async_setup_component(self.hass, "switch", {})
        for entity_id in self.source_ids:
            if self.hass.states.get(entity_id) is None:
                continue
            try:
                await self.hass.services.async_call("switch", service, {"entity_id": entity_id}, blocking=True)
            except Exception as err:  # noqa: BLE001 - a failed Scheduler call must never take the controller down
                ok = False
                _LOGGER.warning("%s: switch.%s on %s failed: %s", self.room_name, service, entity_id, err)
        return ok

    async def _take_over(self, now: datetime) -> None:
        if not await self._call_sources("turn_off"):
            return  # retried on the next evaluation
        self._took_over = True
        self._took_over_at = now
        self._diagnostic("scheduler_takeover", f"Hearth owns this room's schedule; switched off {', '.join(self.source_ids)}")
        self._arm_enabled_timer(now)

    async def _hand_back(self, reason: str) -> None:
        self._cancel_enabled_timer()
        self._took_over = False
        self._took_over_at = None
        await self._call_sources("turn_on")
        self._set_issue(ISSUE_SOURCE_ENABLED, None)
        self._diagnostic("scheduler_handback", f"Handed the room back to {', '.join(self.source_ids)} ({reason})")

    # ------------------------------------------------------------- "two brains" check

    def _arm_enabled_timer(self, now: datetime) -> None:
        self._cancel_enabled_timer()
        delay = timedelta(seconds=float(self.const("scheduler_enabled_check_s")))
        self._enabled_timer = async_track_point_in_utc_time(self.hass, self._on_enabled_timer, dt_util.as_utc(now + delay))

    def _cancel_enabled_timer(self) -> None:
        if self._enabled_timer is not None:
            self._enabled_timer()
            self._enabled_timer = None

    @callback
    def _on_enabled_timer(self, _now) -> None:
        self._enabled_timer = None
        self._sync_issues(dt_util.utcnow())

    def _enabled_offenders(self, now: datetime) -> list[str]:
        """Sources still on a while after Hearth took the room over."""
        if not self._took_over or self._took_over_at is None:
            return []
        since = max(self._took_over_at, self._source_reenabled_at or self._took_over_at)
        if now - since < timedelta(seconds=float(self.const("scheduler_enabled_check_s"))):
            return []
        out = []
        for entity_id in self.source_ids:
            state = self.hass.states.get(entity_id)
            if state is not None and state.state not in (STATE_OFF, STATE_UNAVAILABLE, STATE_UNKNOWN):
                out.append(entity_id)
        return out

    # ------------------------------------------------------------- repairs

    def _placeholders(self, entities: list[str] | tuple[str, ...], detail: str = "") -> dict[str, str]:
        return {"room": self.room_name, "entities": ", ".join(entities), "detail": detail}

    def _sync_issues(self, now: datetime) -> None:
        if not self.source_ids:
            return
        by_key = {i.key: i for i in self._src_issues}
        for key in (ISSUE_DAY_CONFLICT, ISSUE_UNSUPPORTED_ACTION):
            issue = by_key.get(key)
            self._set_issue(key, self._placeholders(issue.entity_ids, issue.detail) if issue else None)
        if self._shared_state == SHARED_BLOCKED:
            self._set_issue(ISSUE_SHARED_SOURCE, self._placeholders(self.source_ids, ", ".join(self._shared_offenders)))
        else:
            self._set_issue(ISSUE_SHARED_SOURCE, None)
        offenders = self._enabled_offenders(now)
        self._set_issue(ISSUE_SOURCE_ENABLED, self._placeholders(offenders) if offenders else None)

    def _set_issue(self, key: str, placeholders: dict[str, str] | None) -> None:
        current = self._issue_state.get(key)
        if placeholders is None:
            if current is not None:
                ir.async_delete_issue(self.hass, DOMAIN, _issue_id(key, self.entry_id))
                del self._issue_state[key]
            return
        if current == placeholders:
            return
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            _issue_id(key, self.entry_id),
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=key,
            translation_placeholders=placeholders,
        )
        self._issue_state[key] = placeholders

    # ------------------------------------------------------------- diagnostics

    def source_diagnostics(self) -> dict[str, Any]:
        return {
            "source": self.schedule_source,
            "entities": self.source_ids,
            "blocked_reason": self.source_blocked_reason,
            "took_over": self._took_over,
            "derived_at": self.derived_at.isoformat() if self.derived_at else None,
            "missing": list(self._src_missing),
            "issues": [{"key": i.key, "entities": list(i.entity_ids), "detail": i.detail} for i in self._src_issues],
            "shared": {"state": self._shared_state, "offenders": self._shared_offenders},
            "last_good": self._src_good["schedule"] if self._src_good else None,
        }


async def async_remove_source_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Entry removed: switch the source schedules back on (Hearth is gone) and clear its repairs."""
    raw = entry.options.get(CONF_SCHEDULER_ENTITIES) or []
    for entity_id in dict.fromkeys(str(e) for e in raw if e):
        if hass.states.get(entity_id) is None:
            continue
        try:
            await hass.services.async_call("switch", "turn_on", {"entity_id": entity_id}, blocking=True)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Hearth: switch.turn_on on %s failed while removing the room: %s", entity_id, err)
    for key in ALL_ISSUES:
        ir.async_delete_issue(hass, DOMAIN, _issue_id(key, entry.entry_id))
