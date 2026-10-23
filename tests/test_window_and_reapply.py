"""0.7.3: schedule blocks apply under an open window (D57), a VTherm restoring its preset temperature is not an override
(D58), and a bypassed window is not dormant (D59)."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .conftest import VTHERM, make_entry, seed_store, set_numbers, set_vtherm, setup_room, vtherm_attrs
from .test_forecast_skip import local
from .test_schedule import opts, tick

DORMANT = "binary_sensor.hearth_living_room_dormant"


async def test_schedule_block_is_applied_while_the_window_is_open(hass: HomeAssistant, hass_storage, services, freezer) -> None:
    today = dt_util.start_of_local_day()
    await tick(hass, freezer, local(today, 8, 0))
    seed_store(hass_storage, t_rm=10.0, settings={"schedule": True})
    set_numbers(hass)
    set_vtherm(hass, preset="comfort", current=19.0, target=7.0, window="on")
    room = await setup_room(hass, make_entry(options=opts()))
    assert room.dormant_reason == "window"
    await tick(hass, freezer, local(today, 9, 0, 3))
    assert services["set_preset"][-1].data == {"entity_id": VTHERM, "preset_mode": "eco"}
    assert room.sched["applied_key"].endswith("|eco")


async def test_other_dormant_reasons_still_hold_the_schedule(hass: HomeAssistant, hass_storage, services, freezer) -> None:
    today = dt_util.start_of_local_day()
    await tick(hass, freezer, local(today, 8, 0))
    seed_store(hass_storage, t_rm=10.0, settings={"schedule": True})
    set_numbers(hass)
    set_vtherm(hass, preset="comfort", current=19.0, target=20.5, safety="on")
    room = await setup_room(hass, make_entry(options=opts()))
    assert room.dormant_reason == "safety"
    await tick(hass, freezer, local(today, 9, 0, 3))
    assert services["set_preset"] == []


async def _learning_room(hass: HomeAssistant, hass_storage, freezer):
    today = dt_util.start_of_local_day()
    await tick(hass, freezer, local(today, 15, 0))
    seed_store(hass_storage, t_rm=14.0, settings={"learning": True}, extra={"last_written": {"comfort": 21.0}})
    set_numbers(hass, comfort=21.0)
    set_vtherm(hass, preset="comfort", current=19.0, target=7.0, window="on")
    room = await setup_room(hass, make_entry())
    freezer.tick(timedelta(minutes=10))  # the numbers were last touched well outside the match window
    return room


async def test_window_closing_back_to_the_preset_temperature_is_not_an_override(
    hass: HomeAssistant, hass_storage, services, freezer
) -> None:
    room = await _learning_room(hass, hass_storage, freezer)
    set_vtherm(hass, preset="comfort", current=19.0, target=21.0, window="off")
    await hass.async_block_till_done()
    assert not room.standdown_active
    assert room.learning["ignored"][-1]["reason"] == "vtherm_reapply"
    assert room.ledger.entries == []


async def test_options_reload_restoring_the_preset_temperature_is_not_an_override(
    hass: HomeAssistant, hass_storage, services, freezer
) -> None:
    """2026-10-23: window feature switched off, the VTherm reloaded on 7 C and then moved itself to comfort's 20.5."""
    room = await _learning_room(hass, hass_storage, freezer)
    set_vtherm(hass, preset="comfort", current=19.0, target=7.0, window="off")
    await hass.async_block_till_done()
    set_vtherm(hass, preset="comfort", current=19.0, target=21.0, window="off")
    await hass.async_block_till_done()
    assert not room.standdown_active
    assert room.learning["ignored"][-1]["reason"] == "vtherm_reapply"


async def test_a_target_away_from_the_preset_temperature_is_still_an_override(hass: HomeAssistant, hass_storage, services, freezer) -> None:
    room = await _learning_room(hass, hass_storage, freezer)
    set_vtherm(hass, preset="comfort", current=19.0, target=21.0, window="off")
    await hass.async_block_till_done()
    set_vtherm(hass, preset="comfort", current=19.0, target=22.5, window="off")
    await hass.async_block_till_done()
    assert room.standdown_active and room.standdown.cause == "automation_temp"


async def test_a_bypassed_window_is_not_dormant(hass: HomeAssistant, hass_storage, services, freezer) -> None:
    today = dt_util.start_of_local_day()
    await tick(hass, freezer, local(today, 8, 0))
    seed_store(hass_storage, t_rm=10.0)
    set_numbers(hass)
    attrs = vtherm_attrs(preset="comfort", current=19.0, target=20.5, window="on")
    attrs["window_manager"] = {"window_state": "on", "is_window_bypass": True}
    hass.states.async_set(VTHERM, "heat", attrs)
    room = await setup_room(hass, make_entry(options=opts()))
    assert room.dormant_reason is None and hass.states.get(DORMANT).state == "off"
    attrs["window_manager"] = {"window_state": "on", "is_window_bypass": False}
    hass.states.async_set(VTHERM, "heat", {**attrs})
    await hass.async_block_till_done()
    assert room.dormant_reason == "window"
