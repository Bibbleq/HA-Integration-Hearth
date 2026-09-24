"""Phase 1: adaptive comfort offset end to end through HA."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.hearth.const import DOMAIN

from .conftest import COMFORT_NUMBER, VTHERM, make_entry, seed_store, set_numbers, set_vtherm, setup_room


async def test_setup_creates_entities_and_seeds(hass: HomeAssistant, services) -> None:
    set_vtherm(hass, outdoor=8.0)
    set_numbers(hass)
    room = await setup_room(hass, make_entry())
    assert room.running_mean.t_rm == 8.0
    assert room.seed_hold_active
    assert hass.states.get("sensor.hearth_living_room_running_mean_outdoor").state == "8.0"
    assert hass.states.get("switch.hearth_living_room_adaptive_comfort").state == "on"
    assert hass.states.get("switch.hearth_living_room_forecast_skip").state == "off"
    assert hass.states.get("switch.hearth_active").state == "on"
    assert hass.states.get("binary_sensor.hearth_living_room_dormant").state == "off"
    assert hass.states.get("number.hearth_living_room_adaptive_slope").state == "0.15"
    # Seed hold: no write during the first 48 h
    assert services["set_value"] == []
    comfort = hass.states.get("sensor.hearth_living_room_comfort_target")
    assert comfort.state == "20.5"
    assert comfort.attributes["held"] is True


async def test_adaptive_writes_comfort_preset_after_hold(hass: HomeAssistant, hass_storage, services) -> None:
    # T_rm 14 -> offset +0.6 -> 21.1 -> 21.0 written
    seed_store(hass_storage, t_rm=14.0)
    set_vtherm(hass, outdoor=12.0)
    set_numbers(hass, comfort=20.5)
    room = await setup_room(hass, make_entry())
    assert not room.seed_hold_active
    assert len(services["set_value"]) == 1
    call = services["set_value"][0]
    assert call.data == {"entity_id": COMFORT_NUMBER, "value": 21.0}
    assert room.last_written["comfort"] == 21.0
    assert hass.states.get("sensor.hearth_living_room_comfort_target").attributes["quantised"] == 21.0
    # Re-evaluating does not write again
    await room.async_evaluate("test")
    assert len(services["set_value"]) == 1


async def test_adaptive_respects_band_and_max_offset(hass: HomeAssistant, hass_storage, services) -> None:
    seed_store(hass_storage, t_rm=40.0)  # requested +4.5, capped to +1.5, band max 22.0 -> 22.0
    set_vtherm(hass)
    set_numbers(hass, comfort=20.5)
    room = await setup_room(hass, make_entry())
    assert services["set_value"][0].data["value"] == 22.0
    # Never above band even with a silly max_offset
    await room.async_set_setting("max_offset", 5.0)
    assert all(c.data["value"] <= 22.0 for c in services["set_value"])
    seed_store(hass_storage, t_rm=-40.0)


async def test_adaptive_never_below_band(hass: HomeAssistant, hass_storage, services) -> None:
    seed_store(hass_storage, t_rm=-40.0)
    set_vtherm(hass)
    set_numbers(hass, comfort=20.5)
    room = await setup_room(hass, make_entry())
    await room.async_set_setting("max_offset", 5.0)
    assert services["set_value"]
    assert all(c.data["value"] >= 18.0 for c in services["set_value"])


async def test_no_write_when_dormant(hass: HomeAssistant, hass_storage, services) -> None:
    seed_store(hass_storage, t_rm=14.0)
    set_vtherm(hass, window="on")
    set_numbers(hass, comfort=20.5)
    room = await setup_room(hass, make_entry())
    assert room.dormant_reason == "window"
    assert hass.states.get("binary_sensor.hearth_living_room_dormant").state == "on"
    assert hass.states.get("binary_sensor.hearth_living_room_dormant").attributes["reason"] == "window"
    assert services["set_value"] == []
    # Window closes -> write happens
    set_vtherm(hass, window="off")
    await hass.async_block_till_done()
    assert room.dormant_reason is None
    assert len(services["set_value"]) == 1


async def test_dormant_reasons(hass: HomeAssistant, hass_storage, services) -> None:
    seed_store(hass_storage, t_rm=14.0)
    set_vtherm(hass, presence="off")
    set_numbers(hass)
    room = await setup_room(hass, make_entry())
    assert room.dormant_reason == "away"
    # A frost preset on its own is not dormant (schedule blocks and hand-set frost)
    set_vtherm(hass, preset="frost")
    await hass.async_block_till_done()
    assert room.dormant_reason is None
    # VTherm central "Frost protection" (holiday) is
    set_vtherm(hass, preset="frost", central_mode="Frost protection")
    await hass.async_block_till_done()
    assert room.dormant_reason == "central_frost"
    set_vtherm(hass, preset="frost", central_mode="Auto")
    await hass.async_block_till_done()
    assert room.dormant_reason is None
    set_vtherm(hass, safety="on")
    await hass.async_block_till_done()
    assert room.dormant_reason == "safety"
    set_vtherm(hass, overpowering="on")
    await hass.async_block_till_done()
    assert room.dormant_reason == "overpowering"
    set_vtherm(hass, state="off")
    await hass.async_block_till_done()
    assert room.dormant_reason == "off"
    hass.states.async_set(VTHERM, "unavailable")
    await hass.async_block_till_done()
    assert room.dormant_reason == "unavailable"
    # Only the two in-scope moments (plain frost, central Auto) may write the comfort number
    assert all(c.data["entity_id"] == COMFORT_NUMBER for c in services["set_value"])


async def test_global_switch_off_disables_everything(hass: HomeAssistant, hass_storage, services) -> None:
    seed_store(hass_storage, t_rm=14.0)
    set_vtherm(hass)
    set_numbers(hass, comfort=20.5)
    room = await setup_room(hass, make_entry())
    assert len(services["set_value"]) == 1
    await hass.services.async_call("switch", "turn_off", {"entity_id": "switch.hearth_active"}, blocking=True)
    await hass.async_block_till_done()
    assert room.dormant_reason == "disabled"
    assert hass.states.get("switch.hearth_active").state == "off"
    await room.async_set_setting("adaptive_slope", 0.4)  # would change the target
    assert len(services["set_value"]) == 1
    await hass.services.async_call("switch", "turn_on", {"entity_id": "switch.hearth_active"}, blocking=True)
    await hass.async_block_till_done()
    assert len(services["set_value"]) == 2


async def test_mechanism_switch_off_stops_writes(hass: HomeAssistant, hass_storage, services) -> None:
    seed_store(hass_storage, t_rm=14.0, settings={"adaptive": False})
    set_vtherm(hass)
    set_numbers(hass, comfort=20.5)
    room = await setup_room(hass, make_entry())
    assert hass.states.get("switch.hearth_living_room_adaptive_comfort").state == "off"
    assert services["set_value"] == []
    # Sensor still computes
    assert hass.states.get("sensor.hearth_living_room_comfort_target").attributes["quantised"] == 21.0
    await hass.services.async_call("switch", "turn_on", {"entity_id": "switch.hearth_living_room_adaptive_comfort"}, blocking=True)
    await hass.async_block_till_done()
    assert len(services["set_value"]) == 1
    assert room.switch("adaptive")


async def test_restart_is_idempotent(hass: HomeAssistant, hass_storage, services) -> None:
    """Store says we already wrote 21.0 and the number shows it: no write on restart."""
    seed_store(hass_storage, t_rm=14.0, extra={"last_written": {"comfort": 21.0}})
    set_vtherm(hass)
    set_numbers(hass, comfort=21.0)
    await setup_room(hass, make_entry())
    assert services["set_value"] == []


async def test_manual_edit_stands_until_target_changes(hass: HomeAssistant, hass_storage, services) -> None:
    seed_store(hass_storage, t_rm=14.0, extra={"last_written": {"comfort": 21.0}})
    set_vtherm(hass)
    set_numbers(hass, comfort=19.5)  # someone turned it down by hand
    room = await setup_room(hass, make_entry())
    assert services["set_value"] == []
    # The target moves -> Hearth writes again
    await room.async_set_setting("adaptive_slope", 0.3)  # offset +1.2 -> 21.7 -> 21.5
    assert services["set_value"][-1].data["value"] == 21.5


async def test_recompute_service_forces_write(hass: HomeAssistant, hass_storage, services) -> None:
    seed_store(hass_storage, t_rm=14.0, extra={"last_written": {"comfort": 21.0}})
    set_vtherm(hass)
    set_numbers(hass, comfort=19.5)
    await setup_room(hass, make_entry())
    await hass.services.async_call(DOMAIN, "recompute", {"vtherm_entity_id": VTHERM}, blocking=True)
    await hass.async_block_till_done()
    assert services["set_value"][-1].data["value"] == 21.0


async def test_daily_roll_updates_running_mean(hass: HomeAssistant, hass_storage, services, freezer) -> None:
    seed_store(hass_storage, t_rm=10.0)
    set_vtherm(hass, outdoor=20.0)
    set_numbers(hass)
    room = await setup_room(hass, make_entry())
    today = dt_util.now().date()
    # Samples today at 20 C
    assert room.running_mean.current.count == 1
    # Just after midnight, before the 00:10 recompute: nothing rolls yet
    early = dt_util.start_of_local_day() + timedelta(days=1, minutes=5)
    freezer.move_to(early)
    async_fire_time_changed(hass, early)
    await hass.async_block_till_done()
    assert room.running_mean.t_rm == 10.0
    # 00:15: the tick rolls yesterday's mean in
    tomorrow = dt_util.start_of_local_day() + timedelta(minutes=15)
    freezer.move_to(tomorrow)
    async_fire_time_changed(hass, tomorrow)
    await hass.async_block_till_done()
    assert room.running_mean.t_rm == 12.0  # 0.2*20 + 0.8*10
    assert room.running_mean.t_rm_day == today.isoformat()
    assert room.running_mean.history == [20.0]
    # Rolling again changes nothing
    later = tomorrow + timedelta(minutes=5)
    freezer.move_to(later)
    async_fire_time_changed(hass, later)
    await hass.async_block_till_done()
    assert room.running_mean.t_rm == 12.0


async def test_outdoor_unavailable_freezes(hass: HomeAssistant, hass_storage, services) -> None:
    seed_store(hass_storage, t_rm=14.0)
    set_vtherm(hass, outdoor=None)
    set_numbers(hass)
    events = []
    hass.bus.async_listen("hearth_diagnostic", lambda e: events.append(e.data))
    room = await setup_room(hass, make_entry())
    assert room.running_mean.frozen
    assert any(e["kind"] == "outdoor_unavailable" for e in events)
    assert hass.states.get("sensor.hearth_living_room_comfort_target").attributes["held"] is True
    assert services["set_value"] == []


async def test_missing_preset_numbers_is_dormant(hass: HomeAssistant, hass_storage, services) -> None:
    seed_store(hass_storage, t_rm=14.0)
    set_vtherm(hass)
    room = await setup_room(hass, make_entry())
    assert room.dormant_reason == "no_preset_entities"
    assert services["set_value"] == []


async def test_unload(hass: HomeAssistant, services) -> None:
    set_vtherm(hass)
    set_numbers(hass)
    entry = make_entry()
    await setup_room(hass, entry)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.entry_id not in hass.data[DOMAIN]["rooms"]
