"""Scheduler entities as a room's schedule source: options, derivation, take-over and fallback, repairs."""

from __future__ import annotations

import logging
from datetime import timedelta

import pytest
from homeassistant import config_entries
from homeassistant.const import EVENT_CALL_SERVICE
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_capture_events, async_fire_time_changed

from custom_components.hearth.const import (
    CONF_BAND_MAX,
    CONF_BAND_MIN,
    CONF_BASE_TEMP,
    CONF_ROOM_NAME,
    CONF_SCHEDULER_ENTITIES,
    CONF_SKIPPABLE_FIRST_MORNING,
    CONF_VTHERM,
    CONF_WARM_BY_PRESETS,
    DOMAIN,
    EVENT_DIAGNOSTIC,
    ISSUE_DAY_CONFLICT,
    ISSUE_SHARED_SOURCE,
    ISSUE_SOURCE_ENABLED,
    ISSUE_UNSUPPORTED_ACTION,
)

from .conftest import VTHERM, make_entry, seed_store, set_numbers, set_vtherm, setup_room
from .test_forecast_skip import local

WEEKDAY_ID = "switch.schedule_living_room_weekday"
WEEKEND_ID = "switch.schedule_living_room_weekend"
BEDROOM = "climate.bedroom"
NEXT_BLOCK = "sensor.hearth_living_room_next_block"
SOURCES = [WEEKDAY_ID, WEEKEND_ID]


def act(preset: str) -> dict:
    return {"service": "climate.set_preset_mode", "data": {"preset_mode": preset}}


WEEKDAY_ATTRS = {
    "weekdays": ["mon", "tue", "wed", "thu", "fri"],
    "timeslots": ["00:00:00 - 06:30:00", "06:30:00 - 09:00:00", "09:00:00 - 16:00:00", "16:00:00 - 22:00:00", "22:00:00 - 00:00:00"],
    "actions": [act("eco"), act("comfort"), act("eco"), act("comfort"), act("eco")],
    "entities": [VTHERM],
}
WEEKEND_ATTRS = {
    "weekdays": ["sat", "sun"],
    "timeslots": ["00:00:00 - 06:45:00", "06:45:00 - 22:00:00", "22:00:00 - 00:00:00"],
    "actions": [act("eco"), act("comfort"), act("eco")],
    "entities": [VTHERM],
}
WEEKDAY_BLOCKS = [
    {"at": "00:00", "preset": "eco"},
    {"warm_by": "06:30", "preset": "comfort", "skippable": True},
    {"at": "09:00", "preset": "eco"},
    {"warm_by": "16:00", "preset": "comfort"},
    {"at": "22:00", "preset": "eco"},
]
WEEKEND_BLOCKS = [
    {"at": "00:00", "preset": "eco"},
    {"warm_by": "06:45", "preset": "comfort", "skippable": True},
    {"at": "22:00", "preset": "eco"},
]


class FakeScheduler:
    """Stands in for the Scheduler component: switch states with schedule attributes, records on/off calls."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.calls: list[tuple[str, str]] = []
        self.entities: set[str] = set()
        self.stuck: set[str] = set()  # sources that ignore turn_off, to play a switch call that keeps failing
        hass.bus.async_listen(EVENT_CALL_SERVICE, self._on_call)

    def add(self, entity_id: str, attrs: dict, state: str = "on") -> None:
        self.entities.add(entity_id)
        self.hass.states.async_set(entity_id, state, attrs)

    def attrs(self, entity_id: str) -> dict:
        return dict(self.hass.states.get(entity_id).attributes)

    def update(self, entity_id: str, **changes) -> None:
        state = self.hass.states.get(entity_id)
        self.hass.states.async_set(entity_id, state.state, {**state.attributes, **changes})

    def set_state(self, entity_id: str, state: str) -> None:
        self.hass.states.async_set(entity_id, state, self.hass.states.get(entity_id).attributes)

    @callback
    def _on_call(self, event: Event) -> None:
        data = event.data
        if data["domain"] != "switch" or data["service"] not in ("turn_on", "turn_off"):
            return
        ids = data["service_data"].get("entity_id")
        for entity_id in [ids] if isinstance(ids, str) else (ids or []):
            if entity_id in self.entities:
                self.calls.append((data["service"], entity_id))
                if data["service"] == "turn_off" and entity_id in self.stuck:
                    continue
                self.set_state(entity_id, "on" if data["service"] == "turn_on" else "off")

    def calls_for(self, service: str) -> list[str]:
        return [e for s, e in self.calls if s == service]

    def clear(self) -> None:
        self.calls.clear()


@pytest.fixture
async def env(hass: HomeAssistant, services) -> FakeScheduler:
    assert await async_setup_component(hass, "switch", {})
    scheduler = FakeScheduler(hass)
    scheduler.add(WEEKDAY_ID, WEEKDAY_ATTRS)
    scheduler.add(WEEKEND_ID, WEEKEND_ATTRS)
    set_vtherm(hass, preset="eco", current=18.0)
    set_numbers(hass)
    return scheduler


def source_options(**extra) -> dict:
    return {CONF_SCHEDULER_ENTITIES: list(SOURCES), **extra}


def issue(hass: HomeAssistant, key: str, entry: MockConfigEntry):
    return ir.async_get(hass).async_get_issue(DOMAIN, f"{key}_{entry.entry_id}")


def blob(room) -> dict:
    return room.schedule_model.to_dict()


async def settle(hass: HomeAssistant) -> None:
    await hass.async_block_till_done()


# ------------------------------------------------------------------ options


async def test_options_exist_in_config_and_options_flow(hass: HomeAssistant, services) -> None:
    set_vtherm(hass)
    set_numbers(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_VTHERM: VTHERM})
    assert result["step_id"] == "settings"
    keys = {str(k): k for k in result["data_schema"].schema}
    assert {CONF_SCHEDULER_ENTITIES, CONF_WARM_BY_PRESETS, CONF_SKIPPABLE_FIRST_MORNING} <= set(keys)
    # Nothing supplied for the new fields: defaults apply and the optional entity list is absent.
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_BASE_TEMP: 20.5, CONF_BAND_MIN: 18.0, CONF_BAND_MAX: 22.0, "affected_presets": ["comfort"]}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["options"][CONF_WARM_BY_PRESETS] == ["comfort", "boost"]
    assert result["options"][CONF_SKIPPABLE_FIRST_MORNING] is True
    assert CONF_SCHEDULER_ENTITIES not in result["options"]


async def test_config_flow_stores_scheduler_entities(hass: HomeAssistant, services) -> None:
    set_vtherm(hass)
    set_numbers(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_VTHERM: VTHERM, CONF_ROOM_NAME: "Lounge"})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_SCHEDULER_ENTITIES: SOURCES,
            CONF_WARM_BY_PRESETS: ["eco", "comfort"],
            CONF_SKIPPABLE_FIRST_MORNING: False,
            CONF_BASE_TEMP: 20.5,
            CONF_BAND_MIN: 18.0,
            CONF_BAND_MAX: 22.0,
            "affected_presets": ["comfort"],
        },
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["options"][CONF_SCHEDULER_ENTITIES] == SOURCES
    assert result["options"][CONF_WARM_BY_PRESETS] == ["eco", "comfort"]
    assert result["options"][CONF_SKIPPABLE_FIRST_MORNING] is False


async def test_options_flow_sets_and_clears_sources(hass: HomeAssistant, env) -> None:
    entry = make_entry()
    room = await setup_room(hass, entry)
    assert room.schedule_source == "none"
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert CONF_SCHEDULER_ENTITIES in {str(k) for k in result["data_schema"].schema}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_SCHEDULER_ENTITIES: SOURCES,
            CONF_WARM_BY_PRESETS: ["comfort"],
            CONF_SKIPPABLE_FIRST_MORNING: True,
            CONF_BASE_TEMP: 20.5,
            CONF_BAND_MIN: 18.0,
            CONF_BAND_MAX: 22.0,
            "affected_presets": ["comfort"],
        },
    )
    await hass.async_block_till_done()
    assert entry.options[CONF_SCHEDULER_ENTITIES] == SOURCES
    room = hass.data[DOMAIN]["rooms"][entry.entry_id]
    assert room.schedule_source == "scheduler" and blob(room)["mon"] == [
        {"at": "00:00", "preset": "eco"},
        {"warm_by": "06:30", "preset": "comfort", "skippable": True},
        {"at": "09:00", "preset": "eco"},
        {"warm_by": "16:00", "preset": "comfort"},
        {"at": "22:00", "preset": "eco"},
    ]
    # Clearing the field removes the option again
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_WARM_BY_PRESETS: ["comfort"],
            CONF_SKIPPABLE_FIRST_MORNING: True,
            CONF_BASE_TEMP: 20.5,
            CONF_BAND_MIN: 18.0,
            CONF_BAND_MAX: 22.0,
            "affected_presets": ["comfort"],
        },
    )
    await hass.async_block_till_done()
    assert CONF_SCHEDULER_ENTITIES not in entry.options
    assert hass.data[DOMAIN]["rooms"][entry.entry_id].schedule_source == "none"


async def test_warm_by_presets_option_changes_block_kinds(hass: HomeAssistant, env) -> None:
    room = await setup_room(hass, make_entry(options=source_options(warm_by_presets=["boost"], skippable_first_morning=False)))
    assert blob(room)["mon"] == [
        {"at": "00:00", "preset": "eco"},
        {"at": "06:30", "preset": "comfort"},
        {"at": "09:00", "preset": "eco"},
        {"at": "16:00", "preset": "comfort"},
        {"at": "22:00", "preset": "eco"},
    ]


# ------------------------------------------------------------------ derivation and live updates


async def test_derives_schedule_at_setup_without_touching_sources(hass: HomeAssistant, env) -> None:
    entry = make_entry(options=source_options())
    room = await setup_room(hass, entry)
    schedule = blob(room)
    for day in ("mon", "tue", "wed", "thu", "fri"):
        assert schedule[day] == WEEKDAY_BLOCKS
    for day in ("sat", "sun", "non_workday"):
        assert schedule[day] == WEEKEND_BLOCKS
    assert env.calls == []  # schedule switch is off: nothing taken over
    attrs = hass.states.get(NEXT_BLOCK).attributes
    assert attrs["schedule_source"] == "scheduler"
    assert attrs["source_entities"] == SOURCES
    assert attrs["source_blocked_reason"] is None
    assert attrs["derived_at"] is not None
    assert attrs["schedule_enabled"] is False
    assert attrs["schedule"]["mon"] == WEEKDAY_BLOCKS


async def test_sources_override_stored_schedule(hass: HomeAssistant, env) -> None:
    stored = {"mon": [{"at": "07:00", "preset": "comfort"}]}
    room = await setup_room(hass, make_entry(options=source_options(schedule=stored)))
    assert blob(room)["mon"] == WEEKDAY_BLOCKS


async def test_schedule_source_attribute_none(hass: HomeAssistant, env) -> None:
    room = await setup_room(hass, make_entry())
    attrs = hass.states.get(NEXT_BLOCK).attributes
    assert attrs["schedule_source"] == "none" and attrs["source_entities"] == [] and attrs["derived_at"] is None
    assert attrs["source_blocked_reason"] is None
    assert room.schedule_model.is_empty


async def test_schedule_source_attribute_explicit(hass: HomeAssistant, env) -> None:
    await setup_room(hass, make_entry(options={"schedule": {"mon": [{"at": "07:00", "preset": "comfort"}]}}))
    attrs = hass.states.get(NEXT_BLOCK).attributes
    assert attrs["schedule_source"] == "explicit" and attrs["source_entities"] == []


async def test_attribute_change_rederives_live(hass: HomeAssistant, env) -> None:
    room = await setup_room(hass, make_entry(options=source_options()))
    before = room.derived_at
    timeslots = ["00:00:00 - 07:00:00", "07:00:00 - 09:00:00", "09:00:00 - 16:00:00", "16:00:00 - 22:00:00", "22:00:00 - 00:00:00"]
    env.update(WEEKDAY_ID, timeslots=timeslots)
    await settle(hass)
    assert blob(room)["mon"][1] == {"warm_by": "07:00", "preset": "comfort", "skippable": True}
    assert room.derived_at >= before
    env.update(WEEKEND_ID, actions=[act("eco"), act("boost"), act("eco")])
    await settle(hass)
    assert blob(room)["sat"][1] == {"warm_by": "06:45", "preset": "boost", "skippable": True}
    assert blob(room)["non_workday"][1]["preset"] == "boost"
    assert hass.states.get(NEXT_BLOCK).attributes["schedule"]["sun"][1]["preset"] == "boost"


async def test_state_only_changes_do_not_change_the_schedule(hass: HomeAssistant, env) -> None:
    room = await setup_room(hass, make_entry(options=source_options()))
    before = blob(room)
    env.update(WEEKDAY_ID, next_trigger="2026-10-23T06:30:00+00:00", current_slot=0)
    await settle(hass)
    assert blob(room) == before


async def test_unavailable_source_keeps_last_good_schedule(hass: HomeAssistant, env) -> None:
    events = async_capture_events(hass, EVENT_DIAGNOSTIC)
    room = await setup_room(hass, make_entry(options=source_options()))
    good = blob(room)
    hass.states.async_set(WEEKDAY_ID, "unavailable")
    await settle(hass)
    assert blob(room) == good
    assert [e.data["kind"] for e in events if e.data["kind"] == "scheduler_source_missing"] == ["scheduler_source_missing"]
    assert WEEKDAY_ID in next(e.data["message"] for e in events if e.data["kind"] == "scheduler_source_missing")
    # No repair for a missing source
    for key in (ISSUE_DAY_CONFLICT, ISSUE_SHARED_SOURCE, ISSUE_UNSUPPORTED_ACTION, ISSUE_SOURCE_ENABLED):
        assert issue(hass, key, room.entry) is None
    # Recovery with changed data re-derives
    env.add(WEEKDAY_ID, {**WEEKDAY_ATTRS, "actions": [act("eco"), act("boost"), act("eco"), act("comfort"), act("eco")]})
    await settle(hass)
    assert blob(room)["mon"][1]["preset"] == "boost"


async def test_removed_source_keeps_last_good_schedule(hass: HomeAssistant, env) -> None:
    room = await setup_room(hass, make_entry(options=source_options()))
    good = blob(room)
    hass.states.async_remove(WEEKEND_ID)
    await settle(hass)
    assert blob(room) == good


async def test_source_missing_at_startup_is_retried_on_first_state_event(hass: HomeAssistant, env) -> None:
    events = async_capture_events(hass, EVENT_DIAGNOSTIC)
    hass.states.async_remove(WEEKDAY_ID)
    hass.states.async_remove(WEEKEND_ID)
    room = await setup_room(hass, make_entry(options=source_options()))
    assert room.schedule_model.is_empty
    assert any(e.data["kind"] == "scheduler_source_missing" for e in events)
    hass.states.async_set(WEEKDAY_ID, "on", WEEKDAY_ATTRS)
    await settle(hass)
    assert room.schedule_model.is_empty  # still waiting for the second source
    hass.states.async_set(WEEKEND_ID, "on", WEEKEND_ATTRS)
    await settle(hass)
    assert blob(room)["mon"] == WEEKDAY_BLOCKS


async def test_last_good_schedule_survives_a_restart_without_scheduler(hass: HomeAssistant, hass_storage, env) -> None:
    entry = make_entry(options=source_options())
    room = await setup_room(hass, entry)
    good = blob(room)
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    hass.states.async_remove(WEEKDAY_ID)
    hass.states.async_remove(WEEKEND_ID)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert blob(hass.data[DOMAIN]["rooms"][entry.entry_id]) == good


async def test_vtherm_not_in_source_entities_warns_and_carries_on(hass: HomeAssistant, env, caplog) -> None:
    env.update(WEEKDAY_ID, entities=["climate.somewhere_else"])
    with caplog.at_level(logging.WARNING):
        room = await setup_room(hass, make_entry(options=source_options()))
    assert blob(room)["mon"] == WEEKDAY_BLOCKS
    assert any(WEEKDAY_ID in r.message and VTHERM in r.message for r in caplog.records if r.levelno == logging.WARNING)
    assert room.source_blocked_reason is None


# ------------------------------------------------------------------ set_schedule


async def test_set_schedule_refused_for_scheduler_rooms(hass: HomeAssistant, env) -> None:
    entry = make_entry(options=source_options())
    room = await setup_room(hass, entry)
    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            DOMAIN, "set_schedule", {"vtherm_entity_id": VTHERM, "schedule": {"mon": [{"at": "07:00", "preset": "comfort"}]}}, blocking=True
        )
    message = str(err.value)
    assert "Scheduler" in message and WEEKDAY_ID in message and WEEKEND_ID in message
    assert "schedule" not in entry.options
    assert blob(room)["mon"] == WEEKDAY_BLOCKS


async def test_set_schedule_still_works_without_sources(hass: HomeAssistant, env) -> None:
    entry = make_entry()
    await setup_room(hass, entry)
    await hass.services.async_call(
        DOMAIN, "set_schedule", {"vtherm_entity_id": VTHERM, "schedule": {"mon": [{"at": "07:00", "preset": "comfort"}]}}, blocking=True
    )
    await hass.async_block_till_done()
    assert entry.options["schedule"] == {"mon": [{"at": "07:00", "preset": "comfort"}]}


# ------------------------------------------------------------------ take-over and fallback


async def test_schedule_switch_on_takes_over_and_off_hands_back(hass: HomeAssistant, env) -> None:
    room = await setup_room(hass, make_entry(options=source_options()))
    assert env.calls == []
    await room.async_set_setting("schedule", True)
    assert sorted(env.calls_for("turn_off")) == sorted(SOURCES)
    assert room.took_over
    assert hass.states.get(WEEKDAY_ID).state == "off"
    env.clear()
    await room.async_set_setting("schedule", False)
    assert sorted(env.calls_for("turn_on")) == sorted(SOURCES)
    assert not room.took_over
    # Idempotent: a second off changes nothing
    env.clear()
    await room.async_set_setting("schedule", False)
    assert env.calls == []


async def test_takeover_through_the_real_schedule_switch_entity(hass: HomeAssistant, env) -> None:
    await setup_room(hass, make_entry(options=source_options()))
    await hass.services.async_call("switch", "turn_on", {"entity_id": "switch.hearth_living_room_schedule"}, blocking=True)
    await hass.async_block_till_done()
    assert sorted(env.calls_for("turn_off")) == sorted(SOURCES)
    env.clear()
    await hass.services.async_call("switch", "turn_off", {"entity_id": "switch.hearth_living_room_schedule"}, blocking=True)
    await hass.async_block_till_done()
    assert sorted(env.calls_for("turn_on")) == sorted(SOURCES)


async def test_setup_with_schedule_switch_on_makes_sources_off(hass: HomeAssistant, hass_storage, env) -> None:
    seed_store(hass_storage, t_rm=10.0, settings={"schedule": True})
    room = await setup_room(hass, make_entry(options=source_options()))
    assert sorted(env.calls_for("turn_off")) == sorted(SOURCES)
    assert room.took_over
    assert env.calls_for("turn_on") == []


async def test_setup_with_sources_already_off_is_harmless(hass: HomeAssistant, hass_storage, env) -> None:
    env.set_state(WEEKDAY_ID, "off")
    env.set_state(WEEKEND_ID, "off")
    seed_store(hass_storage, t_rm=10.0, settings={"schedule": True})
    room = await setup_room(hass, make_entry(options=source_options()))
    assert room.took_over and hass.states.get(WEEKDAY_ID).state == "off"
    assert blob(room)["mon"] == WEEKDAY_BLOCKS  # attributes stay when the switch is off


async def test_global_switch_off_hands_back_and_on_takes_over(hass: HomeAssistant, hass_storage, env) -> None:
    seed_store(hass_storage, t_rm=10.0, settings={"schedule": True})
    room = await setup_room(hass, make_entry(options=source_options()))
    env.clear()
    await hass.data[DOMAIN]["global"].async_set_active(False)
    await hass.async_block_till_done()
    assert sorted(env.calls_for("turn_on")) == sorted(SOURCES) and not room.took_over
    env.clear()
    await hass.data[DOMAIN]["global"].async_set_active(True)
    await hass.async_block_till_done()
    assert sorted(env.calls_for("turn_off")) == sorted(SOURCES) and room.took_over


async def test_schedule_switch_on_while_global_off_does_not_take_over(hass: HomeAssistant, env) -> None:
    room = await setup_room(hass, make_entry(options=source_options()))
    await hass.data[DOMAIN]["global"].async_set_active(False)
    await hass.async_block_till_done()
    await room.async_set_setting("schedule", True)
    assert env.calls == []
    await hass.data[DOMAIN]["global"].async_set_active(True)
    await hass.async_block_till_done()
    assert sorted(env.calls_for("turn_off")) == sorted(SOURCES)


async def test_dormancy_and_standdown_do_not_hand_back(hass: HomeAssistant, hass_storage, env) -> None:
    seed_store(hass_storage, t_rm=10.0, settings={"schedule": True})
    room = await setup_room(hass, make_entry(options=source_options()))
    env.clear()
    set_vtherm(hass, preset="eco", current=18.0, window="on")
    await hass.async_block_till_done()
    assert room.dormant and room.took_over
    set_vtherm(hass, preset="eco", current=18.0, window="off")
    await hass.async_block_till_done()
    await room.async_service_override(60)
    await hass.async_block_till_done()
    assert room.standdown_active and room.took_over
    assert env.calls == []


async def test_unload_hands_back(hass: HomeAssistant, hass_storage, env) -> None:
    seed_store(hass_storage, t_rm=10.0, settings={"schedule": True})
    entry = make_entry(options=source_options())
    await setup_room(hass, entry)
    env.clear()
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert sorted(env.calls_for("turn_on")) == sorted(SOURCES)


async def test_unload_without_takeover_leaves_sources_alone(hass: HomeAssistant, env) -> None:
    entry = make_entry(options=source_options())
    await setup_room(hass, entry)
    env.set_state(WEEKDAY_ID, "off")  # the user's own choice
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert env.calls == []


async def test_remove_entry_hands_back(hass: HomeAssistant, hass_storage, env) -> None:
    seed_store(hass_storage, t_rm=10.0, settings={"schedule": True})
    entry = make_entry(options=source_options())
    await setup_room(hass, entry)
    env.clear()
    assert await hass.config_entries.async_remove(entry.entry_id) is not None
    await hass.async_block_till_done()
    assert sorted(set(env.calls_for("turn_on"))) == sorted(SOURCES)
    assert hass.states.get(WEEKDAY_ID).state == "on"


async def test_reload_via_options_hands_back_then_takes_over_again(hass: HomeAssistant, hass_storage, env) -> None:
    seed_store(hass_storage, t_rm=10.0, settings={"schedule": True})
    entry = make_entry(options=source_options())
    await setup_room(hass, entry)
    env.clear()
    hass.config_entries.async_update_entry(entry, options={**entry.options, CONF_BASE_TEMP: 21.0})
    await hass.async_block_till_done()
    assert env.calls_for("turn_on") and env.calls_for("turn_off")
    assert hass.states.get(WEEKDAY_ID).state == "off"  # taken over again after the reload


async def test_taken_over_room_applies_presets_from_derived_schedule(hass: HomeAssistant, hass_storage, services, env, freezer) -> None:
    today = dt_util.start_of_local_day()
    # Make "today" a weekday so the Mon-Fri entity governs it
    while today.weekday() >= 5:
        today += timedelta(days=1)
    freezer.move_to(local(today, 5, 0))
    seed_store(hass_storage, t_rm=10.0, settings={"schedule": True})
    set_vtherm(hass, preset="eco", current=18.0)
    await setup_room(hass, make_entry(options=source_options()))
    assert services["set_preset"] == []
    freezer.move_to(local(today, 6, 30, 3))
    async_fire_time_changed(hass, local(today, 6, 30, 3))
    await hass.async_block_till_done()
    assert services["set_preset"][-1].data == {"entity_id": VTHERM, "preset_mode": "comfort"}


# ------------------------------------------------------------------ shared-source guard


async def _bedroom_entry(hass: HomeAssistant, *, schedule_on: bool, hass_storage: dict) -> MockConfigEntry:
    hass.states.async_set(
        BEDROOM, "heat", {"friendly_name": "Bedroom", "preset_mode": "eco", "preset_modes": ["none", "frost", "eco", "comfort", "boost"]}
    )
    hass_storage["hearth.room.climate_bedroom"] = {
        "version": 1,
        "key": "hearth.room.climate_bedroom",
        "data": {
            "vtherm": BEDROOM,
            "settings": {"schedule": schedule_on},
            "running_mean": {
                "t_rm": 10.0,
                "t_rm_day": (dt_util.now().date() - timedelta(days=1)).isoformat(),
                "seeded_at": (dt_util.utcnow() - timedelta(days=3)).isoformat(),
                "frozen": False,
            },
            "write_log": [],
            "last_written": {},
            "mechanisms": {},
        },
    }
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Hearth Bedroom",
        unique_id=BEDROOM,
        data={CONF_VTHERM: BEDROOM, CONF_ROOM_NAME: "Bedroom"},
        options={CONF_BASE_TEMP: 20.5, CONF_BAND_MIN: 18.0, CONF_BAND_MAX: 22.0},
    )
    return entry


async def test_shared_source_with_foreign_room_blocks_takeover(hass: HomeAssistant, env) -> None:
    env.update(WEEKDAY_ID, entities=[VTHERM, "climate.not_on_hearth"])
    room = await setup_room(hass, make_entry(options=source_options()))
    await room.async_set_setting("schedule", True)
    assert env.calls == []  # not turned off
    assert not room.took_over
    assert room.schedule_model.is_empty  # derived schedule not applied
    found = issue(hass, ISSUE_SHARED_SOURCE, room.entry)
    assert found is not None and WEEKDAY_ID in found.translation_placeholders["entities"]
    assert "climate.not_on_hearth" in found.translation_placeholders["detail"]
    attrs = hass.states.get(NEXT_BLOCK).attributes
    assert attrs["source_blocked_reason"] == ISSUE_SHARED_SOURCE
    # The foreign room leaves the source: guard clears, take-over happens, repair goes
    env.update(WEEKDAY_ID, entities=[VTHERM])
    await settle(hass)
    assert room.took_over and sorted(env.calls_for("turn_off")) == sorted(SOURCES)
    assert issue(hass, ISSUE_SHARED_SOURCE, room.entry) is None
    assert blob(room)["mon"] == WEEKDAY_BLOCKS


async def test_shared_source_between_two_hearth_rooms_needs_both_schedule_switches(hass: HomeAssistant, hass_storage, env) -> None:
    env.update(WEEKDAY_ID, entities=[VTHERM, BEDROOM])
    env.update(WEEKEND_ID, entities=[VTHERM, BEDROOM])
    living_entry = make_entry(options=source_options())
    living_entry.add_to_hass(hass)
    bedroom_entry = await _bedroom_entry(hass, schedule_on=False, hass_storage=hass_storage)
    bedroom_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(living_entry.entry_id)
    await hass.async_block_till_done()
    living = hass.data[DOMAIN]["rooms"][living_entry.entry_id]
    assert bedroom_entry.entry_id in hass.data[DOMAIN]["rooms"]
    bedroom = hass.data[DOMAIN]["rooms"][bedroom_entry.entry_id]
    await living.async_set_setting("schedule", True)
    assert env.calls == [] and issue(hass, ISSUE_SHARED_SOURCE, living.entry) is not None
    # The bedroom (no sources of its own) turns its schedule switch on: the living room may now take over
    await bedroom.async_set_setting("schedule", True)
    await hass.async_block_till_done()
    assert living.took_over and sorted(env.calls_for("turn_off")) == sorted(SOURCES)
    assert issue(hass, ISSUE_SHARED_SOURCE, living.entry) is None
    # And the bedroom switching off again pulls the living room back to Scheduler
    env.clear()
    await bedroom.async_set_setting("schedule", False)
    await hass.async_block_till_done()
    assert not living.took_over and sorted(env.calls_for("turn_on")) == sorted(SOURCES)
    assert issue(hass, ISSUE_SHARED_SOURCE, living.entry) is not None
    assert living.schedule_model.is_empty


async def test_shared_source_peer_still_loading_is_not_a_repair(hass: HomeAssistant, hass_storage, env) -> None:
    env.update(WEEKDAY_ID, entities=[VTHERM, BEDROOM])
    env.update(WEEKEND_ID, entities=[VTHERM, BEDROOM])
    seed_store(hass_storage, t_rm=10.0, settings={"schedule": True})
    # The living room entry is added first, so it loads while the bedroom entry is still pending
    living_entry = make_entry(options=source_options())
    living_entry.add_to_hass(hass)
    bedroom_entry = await _bedroom_entry(hass, schedule_on=True, hass_storage=hass_storage)
    bedroom_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(living_entry.entry_id)  # also sets up the bedroom entry
    await hass.async_block_till_done()
    living = hass.data[DOMAIN]["rooms"][living_entry.entry_id]
    assert bedroom_entry.entry_id in hass.data[DOMAIN]["rooms"]
    assert living.took_over
    assert issue(hass, ISSUE_SHARED_SOURCE, living_entry) is None


# ------------------------------------------------------------------ repairs


async def test_day_conflict_repair_raised_and_cleared(hass: HomeAssistant, env) -> None:
    env.update(WEEKEND_ID, weekdays=["sat", "sun", "fri"])
    room = await setup_room(hass, make_entry(options=source_options()))
    found = issue(hass, ISSUE_DAY_CONFLICT, room.entry)
    assert found is not None and "fri" in found.translation_placeholders["detail"]
    assert WEEKDAY_ID in found.translation_placeholders["entities"]
    assert room.schedule_model.is_empty  # nothing applied
    assert hass.states.get(NEXT_BLOCK).attributes["source_blocked_reason"] == ISSUE_DAY_CONFLICT
    env.update(WEEKEND_ID, weekdays=["sat", "sun"])
    await settle(hass)
    assert issue(hass, ISSUE_DAY_CONFLICT, room.entry) is None
    assert blob(room)["fri"] == WEEKDAY_BLOCKS
    assert hass.states.get(NEXT_BLOCK).attributes["source_blocked_reason"] is None


async def test_conflict_after_good_derivation_keeps_last_good_schedule(hass: HomeAssistant, env) -> None:
    room = await setup_room(hass, make_entry(options=source_options()))
    good = blob(room)
    env.update(WEEKEND_ID, weekdays=["sat", "sun", "mon"])
    await settle(hass)
    assert issue(hass, ISSUE_DAY_CONFLICT, room.entry) is not None
    assert blob(room) == good


async def test_unsupported_action_repair_raised_and_cleared(hass: HomeAssistant, hass_storage, env) -> None:
    bad_actions = [{"service": "climate.set_temperature", "data": {"temperature": 21}}, act("comfort"), act("eco")]
    env.update(WEEKEND_ID, actions=bad_actions)
    seed_store(hass_storage, t_rm=10.0, settings={"schedule": True})
    room = await setup_room(hass, make_entry(options=source_options()))
    found = issue(hass, ISSUE_UNSUPPORTED_ACTION, room.entry)
    assert found is not None and found.translation_placeholders["entities"] == WEEKEND_ID
    assert "climate.set_temperature" in found.translation_placeholders["detail"]
    # Nothing usable was derived, so Hearth must not take the room over (it would leave it with no schedule)
    assert room.schedule_model.is_empty and not room.took_over and env.calls == []
    assert hass.states.get(NEXT_BLOCK).attributes["source_blocked_reason"] == ISSUE_UNSUPPORTED_ACTION
    env.update(WEEKEND_ID, actions=[act("eco"), act("comfort"), act("eco")])
    await settle(hass)
    assert issue(hass, ISSUE_UNSUPPORTED_ACTION, room.entry) is None
    assert room.took_over and sorted(env.calls_for("turn_off")) == sorted(SOURCES)


async def test_unsupported_action_after_takeover_keeps_last_good_and_stays_taken_over(hass: HomeAssistant, hass_storage, env) -> None:
    seed_store(hass_storage, t_rm=10.0, settings={"schedule": True})
    room = await setup_room(hass, make_entry(options=source_options()))
    good = blob(room)
    env.update(WEEKDAY_ID, actions=[act("eco"), {"service": "light.turn_on"}, act("eco"), act("comfort"), act("eco")])
    await settle(hass)
    assert issue(hass, ISSUE_UNSUPPORTED_ACTION, room.entry) is not None
    assert blob(room) == good and room.took_over


async def test_source_enabled_repair_after_two_minutes(hass: HomeAssistant, hass_storage, env, freezer) -> None:
    seed_store(hass_storage, t_rm=10.0, settings={"schedule": True})
    room = await setup_room(hass, make_entry(options=source_options()))
    assert room.took_over
    # Someone turns a source back on in the Scheduler card, and Hearth's switch.turn_off keeps failing (D48)
    env.stuck.add(WEEKDAY_ID)
    env.set_state(WEEKDAY_ID, "on")
    await settle(hass)
    assert issue(hass, ISSUE_SOURCE_ENABLED, room.entry) is None  # too early: inside the two minute grace
    freezer.move_to(dt_util.utcnow() + timedelta(seconds=121))
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=122))
    await settle(hass)
    found = issue(hass, ISSUE_SOURCE_ENABLED, room.entry)
    assert found is not None and found.translation_placeholders["entities"] == WEEKDAY_ID
    assert hass.states.get(NEXT_BLOCK).attributes["source_blocked_reason"] is None  # not a blocking reason
    # Turning it off clears the repair
    env.stuck.clear()
    env.set_state(WEEKDAY_ID, "off")
    await settle(hass)
    assert issue(hass, ISSUE_SOURCE_ENABLED, room.entry) is None


async def test_source_enabled_repair_cleared_on_handback(hass: HomeAssistant, hass_storage, env, freezer) -> None:
    seed_store(hass_storage, t_rm=10.0, settings={"schedule": True})
    room = await setup_room(hass, make_entry(options=source_options()))
    env.stuck.add(WEEKEND_ID)
    env.set_state(WEEKEND_ID, "on")
    freezer.move_to(dt_util.utcnow() + timedelta(seconds=130))
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=131))
    await settle(hass)
    assert issue(hass, ISSUE_SOURCE_ENABLED, room.entry) is not None
    await room.async_set_setting("schedule", False)
    assert issue(hass, ISSUE_SOURCE_ENABLED, room.entry) is None


async def test_no_repair_when_sources_stay_off(hass: HomeAssistant, hass_storage, env, freezer) -> None:
    seed_store(hass_storage, t_rm=10.0, settings={"schedule": True})
    room = await setup_room(hass, make_entry(options=source_options()))
    freezer.move_to(dt_util.utcnow() + timedelta(seconds=130))
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=131))
    await settle(hass)
    assert issue(hass, ISSUE_SOURCE_ENABLED, room.entry) is None


async def test_repairs_are_cleared_on_unload(hass: HomeAssistant, env) -> None:
    env.update(WEEKEND_ID, weekdays=["sat", "sun", "fri"])
    entry = make_entry(options=source_options())
    room = await setup_room(hass, entry)
    assert issue(hass, ISSUE_DAY_CONFLICT, room.entry) is not None
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert issue(hass, ISSUE_DAY_CONFLICT, entry) is None


async def test_repairs_are_cleared_on_remove(hass: HomeAssistant, env) -> None:
    env.update(WEEKEND_ID, weekdays=["sat", "sun", "fri"])
    entry = make_entry(options=source_options())
    await setup_room(hass, entry)
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert issue(hass, ISSUE_DAY_CONFLICT, entry) is None


async def test_diagnostics_include_scheduler_source(hass: HomeAssistant, env) -> None:
    room = await setup_room(hass, make_entry(options=source_options()))
    info = room.diagnostics()["scheduler_source"]
    assert info["source"] == "scheduler" and info["entities"] == SOURCES and info["blocked_reason"] is None
    assert info["last_good"]["mon"] == WEEKDAY_BLOCKS


# ------------------------------------------------------------------ pause, and sources switched back on (D45, D48)

PAUSED = "switch.hearth_paused"
ACTIVE = "switch.hearth_active"


async def owned_room(hass: HomeAssistant, hass_storage, freezer, **settings):
    """A took-over room at 03:00, inside the eco block on every day of the week."""
    today = dt_util.start_of_local_day()
    freezer.move_to(local(today, 3, 0))
    seed_store(hass_storage, t_rm=10.0, settings={"schedule": True, "learning": True, **settings})
    room = await setup_room(hass, make_entry(options=source_options()))
    assert room.took_over and room.snapshot.preset == "eco"
    return room


async def switch(hass: HomeAssistant, entity_id: str, on: bool) -> None:
    await hass.services.async_call("switch", "turn_on" if on else "turn_off", {"entity_id": entity_id}, blocking=True)
    await hass.async_block_till_done()


async def test_pause_keeps_ownership_and_leaves_the_sources_off(hass: HomeAssistant, hass_storage, env, freezer) -> None:
    room = await owned_room(hass, hass_storage, freezer)
    env.clear()
    await switch(hass, PAUSED, True)
    assert room.dormant_reason == "paused" and room.took_over
    assert env.calls == []
    assert hass.states.get(WEEKDAY_ID).state == "off" and hass.states.get(WEEKEND_ID).state == "off"
    freezer.move_to(dt_util.utcnow() + timedelta(seconds=300))
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=301))
    await settle(hass)
    await switch(hass, PAUSED, False)
    assert env.calls == [] and room.took_over
    assert issue(hass, ISSUE_SOURCE_ENABLED, room.entry) is None


async def test_active_off_hands_back_even_while_paused(hass: HomeAssistant, hass_storage, env, freezer) -> None:
    room = await owned_room(hass, hass_storage, freezer)
    await switch(hass, PAUSED, True)
    env.clear()
    await switch(hass, ACTIVE, False)
    assert not room.took_over and room.dormant_reason == "disabled"
    assert sorted(env.calls_for("turn_on")) == sorted(SOURCES)
    assert hass.states.get(WEEKDAY_ID).state == "on"
    # Active back on while still paused: Hearth owns the room again and stays dormant
    env.clear()
    await switch(hass, ACTIVE, True)
    assert room.took_over and room.dormant_reason == "paused"
    assert sorted(env.calls_for("turn_off")) == sorted(SOURCES)


async def test_pause_survives_a_reload_and_the_sources_stay_off(hass: HomeAssistant, hass_storage, env, freezer) -> None:
    room = await owned_room(hass, hass_storage, freezer)
    await switch(hass, PAUSED, True)
    assert await hass.config_entries.async_reload(room.entry.entry_id)
    await hass.async_block_till_done()
    reloaded = hass.data[DOMAIN]["rooms"][room.entry.entry_id]
    assert reloaded.dormant_reason == "paused" and reloaded.took_over
    assert hass.states.get(WEEKDAY_ID).state == "off" and hass.states.get(WEEKEND_ID).state == "off"
    assert hass.states.get(PAUSED).state == "on"


async def test_a_reenabled_source_is_switched_off_again(hass: HomeAssistant, hass_storage, env, freezer) -> None:
    room = await owned_room(hass, hass_storage, freezer)
    events = async_capture_events(hass, EVENT_DIAGNOSTIC)
    env.clear()
    env.set_state(WEEKDAY_ID, "on")  # the Home Mode automation switches every schedule on
    await settle(hass)
    assert env.calls_for("turn_off") == [WEEKDAY_ID]
    assert hass.states.get(WEEKDAY_ID).state == "off" and room.took_over
    assert any(e.data["kind"] == "scheduler_source_reenabled" and WEEKDAY_ID in e.data["message"] for e in events)
    # Switched off again at once: no repair, even after the two minute check
    freezer.move_to(dt_util.utcnow() + timedelta(seconds=130))
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=131))
    await settle(hass)
    assert issue(hass, ISSUE_SOURCE_ENABLED, room.entry) is None


async def test_the_schedulers_slot_after_the_switch_event_is_not_an_override(
    hass: HomeAssistant, hass_storage, env, services, freezer
) -> None:
    room = await owned_room(hass, hass_storage, freezer)
    hass.data[DOMAIN]["defaults"]["learn_from_automations"] = True  # so only the grace window can explain the absence of a ledger entry
    env.set_state(WEEKDAY_ID, "on")
    await settle(hass)
    assert hass.states.get(WEEKDAY_ID).state == "off"
    # The Scheduler had enabled itself and now fires its slot: comfort, while Hearth's block is eco
    set_vtherm(hass, preset="comfort", current=18.0, target=20.5)
    await settle(hass)
    assert not room.standdown_active and room.ledger.entries == []
    assert room.learning["ignored"][-1]["reason"] == "scheduler_source"
    assert room.last_external_change is None
    assert services["set_preset"][-1].data == {"entity_id": VTHERM, "preset_mode": "eco"}  # Hearth's own block, put back


async def test_the_schedulers_slot_before_the_switch_event_is_taken_back(hass: HomeAssistant, hass_storage, env, services, freezer) -> None:
    room = await owned_room(hass, hass_storage, freezer)
    hass.data[DOMAIN]["defaults"]["learn_from_automations"] = True  # an override that would be learnt, so there is something to undo
    # The write beats the switch event: Hearth reads it as an override first...
    set_vtherm(hass, preset="comfort", current=18.0, target=20.5)
    env.set_state(WEEKDAY_ID, "on")
    await settle(hass)
    # ...and takes it back when the source turns out to have just been switched on
    assert not room.standdown_active and room.ledger.entries == []
    assert room.last_external_change is None and room.last_external_change_at is None
    assert room.learning["ignored"][-1]["reason"] == "scheduler_source" and "last_override" not in room.learning
    assert hass.states.get(WEEKDAY_ID).state == "off"
    assert services["set_preset"][-1].data == {"entity_id": VTHERM, "preset_mode": "eco"}


async def test_a_change_after_the_grace_window_is_a_real_override(hass: HomeAssistant, hass_storage, env, services, freezer) -> None:
    room = await owned_room(hass, hass_storage, freezer)
    env.set_state(WEEKDAY_ID, "on")
    await settle(hass)
    freezer.tick(timedelta(seconds=20))  # source_reenable_grace_s is 15
    set_vtherm(hass, preset="comfort", current=18.0, target=20.5)
    await settle(hass)
    assert room.standdown_active and room.standdown.cause == "automation_preset"
    assert room.learning["ignored"][-1]["reason"] == "automation"


async def test_a_reenabled_source_while_paused_is_switched_off_and_nothing_is_written(
    hass: HomeAssistant, hass_storage, env, services, freezer
) -> None:
    room = await owned_room(hass, hass_storage, freezer)
    await switch(hass, PAUSED, True)
    env.clear()
    env.set_state(WEEKEND_ID, "on")
    set_vtherm(hass, preset="comfort", current=18.0, target=20.5)  # the Scheduler's slot
    await settle(hass)
    assert env.calls_for("turn_off") == [WEEKEND_ID] and hass.states.get(WEEKEND_ID).state == "off"
    assert services["set_preset"] == [] and not room.standdown_active  # dormant: not Hearth's to put back until the pause ends
    await switch(hass, PAUSED, False)
    assert services["set_preset"][-1].data["preset_mode"] == "eco"  # unpausing applies the current block


async def test_a_source_switched_on_while_hearth_does_not_own_the_room_is_left_alone(
    hass: HomeAssistant, hass_storage, env, freezer
) -> None:
    room = await owned_room(hass, hass_storage, freezer)
    await room.async_set_setting("schedule", False)  # handed back: the sources are on
    env.clear()
    env.set_state(WEEKDAY_ID, "off")
    await settle(hass)
    env.set_state(WEEKDAY_ID, "on")
    await settle(hass)
    assert env.calls == [] and hass.states.get(WEEKDAY_ID).state == "on"


async def test_a_switch_off_that_does_not_stick_is_retried_later(hass: HomeAssistant, hass_storage, env, freezer) -> None:
    room = await owned_room(hass, hass_storage, freezer)
    env.stuck.add(WEEKDAY_ID)
    env.clear()
    env.set_state(WEEKDAY_ID, "on")
    await settle(hass)
    assert env.calls_for("turn_off") == [WEEKDAY_ID]  # once, straight away
    freezer.tick(timedelta(seconds=30))
    await room.async_evaluate("tick")
    assert env.calls_for("turn_off") == [WEEKDAY_ID, WEEKDAY_ID]  # and again on a later evaluation
    env.stuck.clear()
    freezer.tick(timedelta(seconds=30))
    await room.async_evaluate("tick")
    assert hass.states.get(WEEKDAY_ID).state == "off"
