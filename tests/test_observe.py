"""Observe-only rooms (D49-D53): learn first, take control later."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.hearth.const import DOMAIN

from .conftest import (
    COMFORT_NUMBER,
    ECO_NUMBER,
    VTHERM,
    WEATHER,
    make_entry,
    room_store_key,
    seed_store,
    set_numbers,
    set_vtherm,
    setup_room,
)
from .conftest import set_vtherm as _set_vtherm
from .test_forecast_skip import install_weather, local, refresh_forecast
from .test_schedule import opts, tick

OBSERVE = "switch.hearth_living_room_observe"
PAUSED = "switch.hearth_paused"
ACTIVE = "switch.hearth_active"
DORMANT = "binary_sensor.hearth_living_room_dormant"
NEXT_BLOCK = "sensor.hearth_living_room_next_block"
COMFORT = "sensor.hearth_living_room_comfort_target"
ALL_ON = {name: True for name in ("adaptive", "skip", "setback", "schedule", "preheat", "learning", "apply_learning")}


async def toggle(hass: HomeAssistant, entity_id: str, on: bool) -> None:
    await hass.services.async_call("switch", "turn_on" if on else "turn_off", {"entity_id": entity_id}, blocking=True)
    await hass.async_block_till_done()


def preset_issue(hass: HomeAssistant, room):
    return ir.async_get(hass).async_get_issue(DOMAIN, f"no_preset_entities_{room.entry_id}")


def writes(services) -> int:
    return len(services["set_value"]) + len(services["set_preset"])


# ---------------------------------------------------------------- is the room new?


async def test_a_new_room_starts_observing(hass: HomeAssistant, hass_storage, services) -> None:
    hass_storage.pop(room_store_key(), None)  # nothing stored yet: a brand-new room
    set_vtherm(hass, preset="comfort", current=19.0)
    set_numbers(hass)
    room = await setup_room(hass, make_entry())
    assert room.new_room and room.observing
    assert hass.states.get(OBSERVE).state == "on"
    assert room.settings == {"observe": True}
    assert not room.dormant  # observing is not a dormant reason
    await toggle(hass, OBSERVE, False)
    assert not room.observing
    assert hass_storage[room_store_key()]["data"]["settings"]["observe"] is False  # going live is saved at once
    # Once it has a store it is no longer new: a reload keeps it live
    assert await hass.config_entries.async_reload(room.entry.entry_id)
    await hass.async_block_till_done()
    reloaded = hass.data[DOMAIN]["rooms"][room.entry.entry_id]
    assert not reloaded.new_room and not reloaded.observing


async def test_an_existing_store_stays_live(hass: HomeAssistant, hass_storage, services) -> None:
    # A 0.6.x store: settings present (possibly empty) and no "observe" key
    seed_store(hass_storage, t_rm=10.0, settings={"adaptive": True})
    set_vtherm(hass, preset="comfort", current=19.0)
    set_numbers(hass)
    room = await setup_room(hass, make_entry())
    assert not room.new_room and not room.observing
    assert hass.states.get(OBSERVE).state == "off"
    assert hass.states.get(OBSERVE).attributes["default"] is False


async def test_an_existing_empty_settings_store_stays_live(hass: HomeAssistant, hass_storage, services) -> None:
    hass_storage[room_store_key()] = {"version": 1, "key": room_store_key(), "data": {"settings": {}}}
    set_vtherm(hass)
    set_numbers(hass)
    room = await setup_room(hass, make_entry())
    assert not room.new_room and not room.observing


# ---------------------------------------------------------------- nothing is written


async def test_observe_blocks_adaptive_but_still_computes_the_target(hass: HomeAssistant, hass_storage, services) -> None:
    seed_store(hass_storage, t_rm=5.0, settings={**ALL_ON, "observe": True})
    set_vtherm(hass, preset="comfort", current=19.0)
    set_numbers(hass, comfort=20.5)
    room = await setup_room(hass, make_entry())
    await room.async_set_setting("adaptive_slope", 0.3)
    await hass.services.async_call(DOMAIN, "recompute", {"force": True}, blocking=True)
    assert services["set_value"] == [] and services["set_preset"] == []
    sensor = hass.states.get(COMFORT)
    assert float(sensor.state) < 20.5 and sensor.attributes["observe_only"] is True
    assert sensor.attributes["quantised"] == 19.0  # the (capped) offset Hearth would write once live
    # Going live writes it; the stored mechanism switches were never touched
    await toggle(hass, OBSERVE, False)
    assert services["set_value"][-1].data == {"entity_id": COMFORT_NUMBER, "value": 19.0}
    assert all(room.settings[name] for name in ALL_ON)
    assert hass.states.get(COMFORT).attributes["observe_only"] is False


async def test_observe_blocks_schedule_and_preheat_but_shows_the_plan(hass: HomeAssistant, hass_storage, services, freezer) -> None:
    today = dt_util.start_of_local_day()
    await tick(hass, freezer, local(today, 4, 0))
    seed_store(hass_storage, t_rm=10.0, settings={**ALL_ON, "observe": True, "adaptive": False})
    set_vtherm(hass, preset="eco", current=19.0, outdoor=5.0)
    set_numbers(hass, comfort=20.5)
    room = await setup_room(hass, make_entry(options=opts()))
    state = hass.states.get(NEXT_BLOCK)
    assert state.state == "comfort by 06:30, preheat est. 04:46"  # what Hearth would do
    assert state.attributes["observe_only"] is True and state.attributes["preheat_start"] is not None
    assert room.preheat_plan is not None
    for at in ((4, 47), (6, 30, 3), (9, 0, 3), (17, 0, 3)):
        await tick(hass, freezer, local(today, *at))
    assert services["set_preset"] == [] and services["set_value"] == []
    # Going live applies the current block (17:00 comfort) at once
    await toggle(hass, OBSERVE, False)
    assert services["set_preset"][-1].data["preset_mode"] == "comfort"


async def test_observe_blocks_skip(hass: HomeAssistant, hass_storage, services, freezer) -> None:
    today = dt_util.start_of_local_day()
    freezer.move_to(local(today, 6, 0))
    seed_store(hass_storage, t_rm=10.0, settings={**ALL_ON, "observe": True})
    set_vtherm(hass, preset="comfort", current=18.0, outdoor=12.0)
    set_numbers(hass)
    hass.states.async_set(WEATHER, "cloudy")
    room = await setup_room(hass, make_entry(options={"weather_entity_id": WEATHER}))
    install_weather(hass, high=19.0)
    await refresh_forecast(hass)
    assert room.forecast_fresh(dt_util.utcnow())  # forecast fetches keep working
    assert hass.states.get("sensor.hearth_living_room_skip_preview").state == "likely"  # and the preview shows the prediction
    freezer.move_to(local(today, 6, 46))
    async_fire_time_changed(hass, local(today, 6, 46))
    await hass.async_block_till_done()
    assert services["set_preset"] == [] and room.skip.get("status") != "active"


async def test_observe_blocks_setback(hass: HomeAssistant, hass_storage, services, freezer) -> None:
    today = dt_util.start_of_local_day()
    freezer.move_to(local(today, 21, 0))
    seed_store(hass_storage, t_rm=10.0, settings={**ALL_ON, "observe": True})
    set_vtherm(hass, preset="eco", current=18.0)
    set_numbers(hass, eco=15.0)
    install_weather(hass, high=8.0, morning_low=-1.0)
    room = await setup_room(hass, make_entry(options={"weather_entity_id": WEATHER}))
    await refresh_forecast(hass)
    freezer.move_to(local(today, 21, 31))
    async_fire_time_changed(hass, local(today, 21, 31))
    await hass.async_block_till_done()
    assert not [c for c in services["set_value"] if c.data["entity_id"] == ECO_NUMBER]
    assert room.setback["active"] is False


async def test_observe_does_not_apply_learned_corrections_but_the_target_shows_them(hass: HomeAssistant, hass_storage, services) -> None:
    seed_store(hass_storage, t_rm=10.0, settings={**ALL_ON, "observe": True})
    set_vtherm(hass, preset="comfort", current=19.0)
    set_numbers(hass, comfort=20.5)
    room = await setup_room(hass, make_entry())
    room.ledger.buckets["baseline_error"].mean = 0.5
    room.ledger.buckets["baseline_error"].pos = 5.0
    room.ledger.buckets["baseline_error"].weight = 5.0
    room.ledger.buckets["baseline_error"].last_at = dt_util.utcnow()
    assert room.learned_target_offset() > 0
    await room.async_evaluate("test")
    assert services["set_value"] == []


# ---------------------------------------------------------------- no stand-down, no ledger


async def test_observe_records_external_changes_but_stands_nothing_down(hass: HomeAssistant, hass_storage, services, freezer) -> None:
    today = dt_util.start_of_local_day()
    await tick(hass, freezer, local(today, 15, 0))
    seed_store(hass_storage, t_rm=14.0, settings={**ALL_ON, "observe": True})
    _set_vtherm(hass, user_id="ben", preset="comfort", current=19.0, target=21.0)
    set_numbers(hass, comfort=21.0)
    room = await setup_room(hass, make_entry())
    _set_vtherm(hass, user_id="ben", preset="boost", current=19.0, target=22.0)
    await hass.async_block_till_done()
    assert room.last_external_change["to"] == "boost" and room.last_external_change_at is not None
    assert not room.standdown_active and room.ledger.entries == []
    # the deliberate stand-down service has nothing to stand down either
    await hass.services.async_call(DOMAIN, "override", {"vtherm_entity_id": VTHERM}, blocking=True)
    assert not room.standdown_active
    # Live again, the same change stands the room down and is learnt
    await toggle(hass, OBSERVE, False)
    freezer.tick(timedelta(minutes=5))
    _set_vtherm(hass, user_id="ben", preset="comfort", current=19.0, target=21.0)
    await hass.async_block_till_done()
    assert room.standdown_active and len(room.ledger.entries) == 1


# ---------------------------------------------------------------- learning continues


async def test_observe_keeps_learning_a_run_started_by_a_scheduler_preset_change(
    hass: HomeAssistant, hass_storage, services, freezer
) -> None:
    """External change, then heating starts, then the run starts: the change that begins the block does not taint it."""
    today = dt_util.start_of_local_day()
    await tick(hass, freezer, local(today, 6, 0))
    seed_store(hass_storage, t_rm=10.0, settings={**ALL_ON, "observe": True})
    set_numbers(hass)
    set_vtherm(hass, preset="eco", current=17.0, target=15.0, outdoor=5.0, hvac_action="idle")
    room = await setup_room(hass, make_entry(options=opts()))
    assert room.heating_run is None
    for i in range(2):
        start = local(today, 6 + i * 2, 0)
        freezer.move_to(start)
        # 1. the Scheduler's slot switches the room to comfort (no user on the context)
        _set_vtherm(hass, preset="comfort", current=19.0, target=20.5, outdoor=5.0, hvac_action="idle")
        await hass.async_block_till_done()
        assert room.last_external_change_at is not None
        # 2. the VTherm starts heating a moment later, which is when the run starts
        freezer.move_to(start + timedelta(seconds=20))
        _set_vtherm(hass, preset="comfort", current=19.0, target=20.5, outdoor=5.0, hvac_action="heating")
        await hass.async_block_till_done()
        assert room.heating_run is not None and not room.heating_run.tainted
        freezer.move_to(start + timedelta(minutes=30))
        _set_vtherm(hass, preset="comfort", current=19.75, target=20.5, outdoor=5.0, hvac_action="heating")
        await hass.async_block_till_done()
        freezer.move_to(start + timedelta(minutes=60))
        _set_vtherm(hass, preset="comfort", current=20.5, target=20.5, outdoor=5.0, hvac_action="heating")
        await hass.async_block_till_done()
        _set_vtherm(hass, preset="comfort", current=20.5, target=20.5, outdoor=5.0, hvac_action="idle")
        await hass.async_block_till_done()
        assert room.heating_run is None
        assert room.warming_model.n_runs == i + 1
        _set_vtherm(hass, preset="eco", current=20.5, target=15.0, outdoor=5.0, hvac_action="idle")
        await hass.async_block_till_done()
    assert services["set_preset"] == [] and services["set_value"] == []
    assert hass.states.get("sensor.hearth_living_room_warming_rate").attributes["evidence_count"] == 2


async def test_a_change_after_the_run_started_still_taints_it(hass: HomeAssistant, hass_storage, services, freezer) -> None:
    today = dt_util.start_of_local_day()
    await tick(hass, freezer, local(today, 6, 0))
    seed_store(hass_storage, t_rm=10.0, settings={**ALL_ON, "observe": True})
    set_numbers(hass)
    _set_vtherm(hass, preset="comfort", current=17.0, target=20.5, outdoor=5.0, hvac_action="heating")
    room = await setup_room(hass, make_entry(options=opts()))
    assert room.heating_run is not None
    freezer.move_to(local(today, 6, 20))
    _set_vtherm(hass, preset="eco", current=17.5, target=15.0, outdoor=5.0, hvac_action="heating")
    await hass.async_block_till_done()
    assert room.heating_run is None and room.warming_model.n_runs == 0


async def test_observing_keeps_the_outdoor_running_mean_going(hass: HomeAssistant, hass_storage, services) -> None:
    hass_storage.pop(room_store_key(), None)
    set_vtherm(hass, preset="comfort", current=19.0, outdoor=7.0)
    set_numbers(hass)
    room = await setup_room(hass, make_entry())
    assert room.observing and room.running_mean.t_rm == 7.0
    assert room.running_mean.current is not None and room.running_mean.current.count >= 1


# ---------------------------------------------------------------- no preset numbers


async def test_no_preset_numbers_while_observing_is_not_dormant_and_learning_works(
    hass: HomeAssistant, hass_storage, services, freezer
) -> None:
    today = dt_util.start_of_local_day()
    await tick(hass, freezer, local(today, 6, 0))
    seed_store(hass_storage, t_rm=10.0, settings={"observe": True, "adaptive": False})
    set_vtherm(hass, preset="comfort", current=17.0, target=20.5, outdoor=5.0, hvac_action="heating")  # no set_numbers
    room = await setup_room(hass, make_entry(options=opts()))
    assert not room.dormant and hass.states.get(DORMANT).state == "off"
    assert hass.states.get(DORMANT).attributes["observe_only"] is True
    assert preset_issue(hass, room) is None
    assert room.heating_run is not None and not room.heating_run.tainted
    freezer.move_to(local(today, 7, 0))
    _set_vtherm(hass, preset="comfort", current=20.5, target=20.5, outdoor=5.0, hvac_action="heating")
    await hass.async_block_till_done()
    assert room.warming_model.n_runs == 1  # D54: counted when the room reaches target


async def test_going_live_without_preset_numbers_is_dormant_with_a_repair_until_they_appear(
    hass: HomeAssistant, hass_storage, services
) -> None:
    seed_store(hass_storage, t_rm=10.0, settings={"observe": True})
    set_vtherm(hass, preset="comfort", current=19.0)
    room = await setup_room(hass, make_entry())
    assert not room.dormant and preset_issue(hass, room) is None
    await toggle(hass, OBSERVE, False)
    assert room.dormant_reason == "no_preset_entities"
    issue = preset_issue(hass, room)
    assert issue is not None and issue.translation_key == "no_preset_entities"
    assert issue.translation_placeholders == {"room": "Living Room", "vtherm": VTHERM}
    # Observing again withdraws the repair
    await toggle(hass, OBSERVE, True)
    assert not room.dormant and preset_issue(hass, room) is None
    await toggle(hass, OBSERVE, False)
    assert preset_issue(hass, room) is not None
    # The numbers appear (the user unticked central configuration): dormancy and the repair clear
    set_numbers(hass)
    await room.async_evaluate("numbers")
    assert not room.dormant and preset_issue(hass, room) is None


async def test_the_repair_is_cleared_when_the_room_is_unloaded(hass: HomeAssistant, hass_storage, services) -> None:
    seed_store(hass_storage, t_rm=10.0)
    set_vtherm(hass)
    room = await setup_room(hass, make_entry())
    assert preset_issue(hass, room) is not None
    assert await hass.config_entries.async_unload(room.entry.entry_id)
    await hass.async_block_till_done()
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"no_preset_entities_{room.entry_id}") is None


# ---------------------------------------------------------------- precedence


async def test_precedence_active_off_then_paused_then_observe(hass: HomeAssistant, hass_storage, services) -> None:
    seed_store(hass_storage, t_rm=10.0, settings={"observe": True})
    set_vtherm(hass)
    set_numbers(hass)
    room = await setup_room(hass, make_entry())
    assert room.dormant_reason is None  # observing alone is not dormancy
    await toggle(hass, PAUSED, True)
    assert room.dormant_reason == "paused" and room.observing
    await toggle(hass, ACTIVE, False)
    assert room.dormant_reason == "disabled"
    await toggle(hass, ACTIVE, True)
    assert room.dormant_reason == "paused"
    await toggle(hass, PAUSED, False)
    assert room.dormant_reason is None and room.observing


async def test_vtherm_dormant_reasons_still_apply_and_taint_runs_while_observing(
    hass: HomeAssistant, hass_storage, services, freezer
) -> None:
    today = dt_util.start_of_local_day()
    await tick(hass, freezer, local(today, 6, 0))
    seed_store(hass_storage, t_rm=10.0, settings={"observe": True, "adaptive": False})
    set_numbers(hass)
    set_vtherm(hass, preset="comfort", current=17.0, target=20.5, outdoor=5.0, hvac_action="heating")
    room = await setup_room(hass, make_entry(options=opts()))
    assert room.heating_run is not None
    freezer.move_to(local(today, 6, 30))
    set_vtherm(hass, preset="comfort", current=17.5, target=20.5, outdoor=5.0, hvac_action="heating", window="on")
    await hass.async_block_till_done()
    assert room.dormant_reason == "window"
    assert room.heating_run is None and room.warming_model.n_runs == 0
    assert hass.states.get(DORMANT).attributes["reason"] == "window"
    assert hass.states.get(DORMANT).attributes["observe_only"] is True


async def test_observe_only_attribute_is_false_on_a_live_room(hass: HomeAssistant, hass_storage, services) -> None:
    seed_store(hass_storage, t_rm=10.0)
    set_vtherm(hass)
    set_numbers(hass)
    await setup_room(hass, make_entry(options=opts()))
    for entity in (COMFORT, NEXT_BLOCK, DORMANT):
        assert hass.states.get(entity).attributes["observe_only"] is False, entity
    assert hass.states.get(OBSERVE).attributes["effective"] is False  # the switch itself is off
