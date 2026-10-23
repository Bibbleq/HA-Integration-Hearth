"""0.7.2: a VTherm restoring itself at startup is not an override (D55); unchanged entities are not rewritten (D56)."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.util import dt as dt_util

from .conftest import VTHERM, make_entry, seed_store, set_numbers, vtherm_attrs
from .conftest import set_vtherm as _set_vtherm
from .test_forecast_skip import local
from .test_schedule import tick

LEARN = {"learning": True}
COMFORT_TARGET = "sensor.hearth_living_room_comfort_target"


def automation(hass: HomeAssistant, **kwargs) -> None:
    _set_vtherm(hass, **kwargs)


def initialising(hass: HomeAssistant) -> None:
    """What a VTherm reports while it starts: off, no preset, is_ready false."""
    attrs = {**vtherm_attrs(preset="none", target=20.5), "is_ready": False, "temperature": None}
    hass.states.async_set(VTHERM, "off", attrs)


async def _setup(hass: HomeAssistant, hass_storage, freezer):
    from .conftest import setup_room

    today = dt_util.start_of_local_day()
    await tick(hass, freezer, local(today, 15, 0))
    seed_store(hass_storage, t_rm=14.0, settings=LEARN, extra={"last_written": {"comfort": 21.0}})
    automation(hass, preset="eco", current=19.0, target=15.0)
    set_numbers(hass, comfort=21.0)
    return today, await setup_room(hass, make_entry())


async def test_restore_from_an_initialising_vtherm_is_not_an_override(hass: HomeAssistant, hass_storage, services, freezer) -> None:
    _today, room = await _setup(hass, hass_storage, freezer)
    initialising(hass)
    await hass.async_block_till_done()
    automation(hass, preset="comfort", current=19.0, target=21.0)
    await hass.async_block_till_done()
    assert not room.standdown_active
    assert room.learning["ignored"][-1]["reason"] == "startup"
    assert room.ledger.entries == []


async def test_restore_from_unavailable_is_not_an_override(hass: HomeAssistant, hass_storage, services, freezer) -> None:
    _today, room = await _setup(hass, hass_storage, freezer)
    hass.states.async_set(VTHERM, "unavailable", {})
    await hass.async_block_till_done()
    automation(hass, preset="comfort", current=19.0, target=21.0)
    await hass.async_block_till_done()
    assert not room.standdown_active
    assert room.learning["ignored"][-1]["reason"] == "startup"


async def test_an_automation_change_on_a_running_system_still_stands_down(hass: HomeAssistant, hass_storage, services, freezer) -> None:
    _today, room = await _setup(hass, hass_storage, freezer)
    automation(hass, preset="comfort", current=19.0, target=21.0)
    await hass.async_block_till_done()
    assert room.standdown_active and room.standdown.cause == "automation_preset"


async def test_grace_window_runs_until_three_minutes_after_ha_has_started(hass: HomeAssistant, hass_storage, services, freezer) -> None:
    hass.set_state(CoreState.not_running)
    try:
        _today, room = await _setup(hass, hass_storage, freezer)
        assert room._startup_grace_until is not None
        # Still starting: a change with no person behind it is a restore
        automation(hass, preset="comfort", current=19.0, target=21.0)
        await hass.async_block_till_done()
        assert not room.standdown_active and room.learning["ignored"][-1]["reason"] == "startup"
        # HA reports started: the window now ends three minutes later
        hass.set_state(CoreState.running)
        hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
        await hass.async_block_till_done()
        freezer.tick(timedelta(seconds=120))
        automation(hass, preset="eco", current=19.0, target=15.0)
        await hass.async_block_till_done()
        assert not room.standdown_active and room.learning["ignored"][-1]["reason"] == "startup"
        freezer.tick(timedelta(seconds=120))
        automation(hass, preset="comfort", current=19.0, target=21.0)
        await hass.async_block_till_done()
        assert room.standdown_active and room.standdown.cause == "automation_preset"
    finally:
        hass.set_state(CoreState.running)


async def test_a_person_changing_the_room_during_the_window_still_counts(hass: HomeAssistant, hass_storage, services, freezer) -> None:
    hass.set_state(CoreState.not_running)
    try:
        _today, room = await _setup(hass, hass_storage, freezer)
        _set_vtherm(hass, user_id="ben", preset="comfort", current=19.0, target=21.0)
        await hass.async_block_till_done()
        assert room.standdown_active and room.standdown.cause == "manual_preset"
    finally:
        hass.set_state(CoreState.running)


async def test_unchanged_entities_are_not_rewritten(hass: HomeAssistant, hass_storage, services, freezer) -> None:
    _today, room = await _setup(hass, hass_storage, freezer)
    hearth = [s.entity_id for s in hass.states.async_all() if s.entity_id.split(".", 1)[1].startswith("hearth_living_room")]
    before = {entity_id: hass.states.get(entity_id).last_reported for entity_id in hearth}
    assert COMFORT_TARGET in before
    freezer.tick(timedelta(seconds=30))
    # A VTherm update that changes nothing Hearth shows (humidity-style noise): no Hearth entity is rewritten
    hass.states.async_set(VTHERM, "heat", {**vtherm_attrs(preset="eco", current=19.0, target=15.0), "current_humidity": 55})
    await hass.async_block_till_done()
    rewritten = [entity_id for entity_id in hearth if hass.states.get(entity_id).last_reported != before[entity_id]]
    assert rewritten == []


async def test_a_real_change_is_still_written(hass: HomeAssistant, hass_storage, services, freezer) -> None:
    _today, room = await _setup(hass, hass_storage, freezer)
    freezer.tick(timedelta(seconds=30))
    _set_vtherm(hass, preset="eco", current=19.0, target=15.0, window="on")
    await hass.async_block_till_done()
    dormant = hass.states.get("binary_sensor.hearth_living_room_dormant")
    assert dormant.state == "on" and dormant.attributes["reason"] == "window"
    _set_vtherm(hass, preset="eco", current=19.0, target=15.0, window="off")
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.hearth_living_room_dormant").state == "off"


async def test_a_switch_written_by_its_own_handler_is_not_left_stale(hass: HomeAssistant, hass_storage, services, freezer) -> None:
    _today, room = await _setup(hass, hass_storage, freezer)
    switch = "switch.hearth_living_room_observe"
    initial = hass.states.get(switch).state
    target = "on" if initial == "off" else "off"
    await hass.services.async_call("switch", f"turn_{target}", {"entity_id": switch}, blocking=True)
    await hass.async_block_till_done()
    assert hass.states.get(switch).state == target
    await hass.services.async_call("switch", f"turn_{initial}", {"entity_id": switch}, blocking=True)
    await hass.async_block_till_done()
    assert hass.states.get(switch).state == initial
    _set_vtherm(hass, preset="eco", current=19.5, target=15.0)
    await hass.async_block_till_done()
    assert hass.states.get(switch).state == initial
