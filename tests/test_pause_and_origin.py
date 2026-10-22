"""The global pause (D45, D46) and telling a person's change from an automation's (D47)."""

from __future__ import annotations

from homeassistant.core import Context, HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.hearth.const import DOMAIN

from .conftest import COMFORT_NUMBER, VTHERM, make_entry, seed_store, set_numbers, set_vtherm, setup_room
from .conftest import set_vtherm as _set_vtherm
from .test_forecast_skip import local
from .test_schedule import opts, tick

PAUSED = "switch.hearth_paused"
ACTIVE = "switch.hearth_active"
DORMANT = "binary_sensor.hearth_living_room_dormant"
STANDDOWN = "sensor.hearth_living_room_stand_down"
LEARN = {"learning": True}


async def toggle(hass: HomeAssistant, entity_id: str, on: bool) -> None:
    await hass.services.async_call("switch", "turn_on" if on else "turn_off", {"entity_id": entity_id}, blocking=True)
    await hass.async_block_till_done()


def human(hass: HomeAssistant, **kwargs) -> None:
    _set_vtherm(hass, user_id="ben", **kwargs)


def automation(hass: HomeAssistant, **kwargs) -> None:
    _set_vtherm(hass, **kwargs)


# ---------------------------------------------------------------- the pause switch


async def test_pause_switch_makes_every_room_dormant_with_reason_paused(hass: HomeAssistant, hass_storage, services) -> None:
    seed_store(hass_storage, t_rm=10.0)
    set_vtherm(hass, preset="comfort", current=19.0)
    set_numbers(hass)
    room = await setup_room(hass, make_entry())
    assert hass.states.get(PAUSED).state == "off" and hass.data[DOMAIN]["global"].paused is False
    assert not room.dormant
    await toggle(hass, PAUSED, True)
    assert hass.states.get(PAUSED).state == "on"
    assert room.dormant and room.dormant_reason == "paused"
    sensor = hass.states.get(DORMANT)
    assert sensor.state == "on" and sensor.attributes["reason"] == "paused" and sensor.attributes["global_paused"] is True
    assert room.diagnostics()["dormant_reason"] == "paused" and room.diagnostics()["global_paused"] is True
    assert "description" in hass.states.get(PAUSED).attributes and "description" in hass.states.get(ACTIVE).attributes
    await toggle(hass, PAUSED, False)
    assert not room.dormant and hass.states.get(DORMANT).state == "off"


async def test_paused_room_writes_nothing(hass: HomeAssistant, hass_storage, services, freezer) -> None:
    today = dt_util.start_of_local_day()
    await tick(hass, freezer, local(today, 5, 0))
    seed_store(hass_storage, t_rm=10.0, settings={"schedule": True})
    set_vtherm(hass, preset="eco", current=18.0)
    set_numbers(hass, comfort=20.5)
    room = await setup_room(hass, make_entry(options=opts()))
    services["set_value"].clear()
    await toggle(hass, PAUSED, True)
    # Neither a block boundary, nor a changed slope, nor a recompute writes while paused
    await tick(hass, freezer, local(today, 6, 30, 3))
    await room.async_set_setting("adaptive_slope", 0.3)
    await hass.services.async_call(DOMAIN, "recompute", {"force": True}, blocking=True)
    await tick(hass, freezer, local(today, 9, 0, 3))
    assert services["set_preset"] == [] and services["set_value"] == []
    assert room.dormant_reason == "paused"


async def test_changes_while_paused_are_ignored_as_dormant(hass: HomeAssistant, hass_storage, services, freezer) -> None:
    today = dt_util.start_of_local_day()
    await tick(hass, freezer, local(today, 15, 0))
    seed_store(hass_storage, t_rm=14.0, settings=LEARN)
    human(hass, preset="comfort", current=19.0, target=20.5)
    set_numbers(hass)
    room = await setup_room(hass, make_entry())
    await toggle(hass, PAUSED, True)
    human(hass, preset="boost", current=19.0, target=22.0)
    await hass.async_block_till_done()
    assert not room.standdown_active and room.ledger.entries == []
    assert room.learning["ignored"][-1]["reason"] == "dormant"
    assert hass.states.get(STANDDOWN).state == "none"


async def test_pause_taints_a_heating_run(hass: HomeAssistant, hass_storage, services, freezer) -> None:
    today = dt_util.start_of_local_day()
    await tick(hass, freezer, local(today, 6, 0))
    seed_store(hass_storage, t_rm=10.0, settings={"adaptive": False})
    set_numbers(hass)
    set_vtherm(hass, preset="comfort", current=17.0, target=20.5, outdoor=5.0, hvac_action="heating")
    room = await setup_room(hass, make_entry())
    assert room.heating_run is not None and not room.heating_run.tainted
    # A tainted run is discarded rather than learnt from (the same path a window opening takes)
    freezer.move_to(local(today, 7, 0))
    await toggle(hass, PAUSED, True)
    set_vtherm(hass, preset="comfort", current=19.0, target=20.5, outdoor=5.0, hvac_action="idle")
    await hass.async_block_till_done()
    assert room.heating_run is None and room.warming_model.n_runs == 0


async def test_pause_state_is_stored_and_restored(hass: HomeAssistant, hass_storage, services) -> None:
    seed_store(hass_storage, t_rm=10.0)
    hass_storage["hearth.global"] = {"version": 1, "key": "hearth.global", "data": {"active": True, "paused": True}}
    set_vtherm(hass, preset="comfort", current=19.0)
    set_numbers(hass)
    entry = make_entry()
    room = await setup_room(hass, entry)
    assert room.dormant_reason == "paused" and hass.states.get(PAUSED).state == "on"
    await toggle(hass, PAUSED, False)
    assert hass_storage["hearth.global"]["data"] == {"active": True, "paused": False}
    await toggle(hass, PAUSED, True)
    assert hass_storage["hearth.global"]["data"] == {"active": True, "paused": True}
    # Reloading the room keeps the pause; so does the active switch's own saved state
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.data[DOMAIN]["rooms"][entry.entry_id].dormant_reason == "paused"
    assert hass.states.get(PAUSED).state == "on"
    await toggle(hass, ACTIVE, False)
    assert hass_storage["hearth.global"]["data"] == {"active": False, "paused": True}


async def test_a_store_from_before_the_pause_loads_as_not_paused(hass: HomeAssistant, hass_storage, services) -> None:
    seed_store(hass_storage, t_rm=10.0)
    hass_storage["hearth.global"] = {"version": 1, "key": "hearth.global", "data": {"active": False}}
    set_vtherm(hass)
    set_numbers(hass)
    await setup_room(hass, make_entry())
    assert hass.data[DOMAIN]["global"].paused is False and hass.data[DOMAIN]["global"].active is False


async def test_active_off_wins_over_the_pause(hass: HomeAssistant, hass_storage, services) -> None:
    seed_store(hass_storage, t_rm=10.0)
    set_vtherm(hass)
    set_numbers(hass)
    room = await setup_room(hass, make_entry())
    await toggle(hass, PAUSED, True)
    await toggle(hass, ACTIVE, False)
    assert room.dormant_reason == "disabled"
    await toggle(hass, ACTIVE, True)
    assert room.dormant_reason == "paused"


# ---------------------------------------------------------------- unpausing


async def test_unpause_applies_the_current_block_at_once(hass: HomeAssistant, hass_storage, services, freezer) -> None:
    today = dt_util.start_of_local_day()
    await tick(hass, freezer, local(today, 10, 0))
    seed_store(hass_storage, t_rm=5.0, settings={"schedule": True, **LEARN})
    set_vtherm(hass, preset="eco", current=18.0)
    set_numbers(hass, comfort=20.5)
    room = await setup_room(hass, make_entry(options=opts()))
    services["set_value"].clear()
    assert services["set_preset"] == []  # 10:00 is in the 09:00 eco block and the room is already eco
    await toggle(hass, PAUSED, True)
    # While paused an occupancy automation warms the room up and Hearth keeps its hands off
    freezer.move_to(local(today, 10, 30))
    automation(hass, preset="comfort", current=18.0, target=20.5)
    await hass.async_block_till_done()
    assert services["set_preset"] == [] and not room.standdown_active
    await room.async_set_setting("adaptive_slope", 0.3)
    assert services["set_value"] == []
    # Unpausing goes straight back: the 09:00 block's preset, and the adaptive comfort recompute
    await toggle(hass, PAUSED, False)
    assert services["set_preset"][-1].data == {"entity_id": VTHERM, "preset_mode": "eco"}
    assert services["set_value"], "the adaptive recompute runs on unpause"
    assert not room.dormant
    # ...and it stays: the next tick does not fight it or re-apply it
    count = len(services["set_preset"])
    automation(hass, preset="eco", current=18.0, target=15.0)
    await tick(hass, freezer, local(today, 11, 0))
    assert len(services["set_preset"]) == count


async def test_unpause_ends_a_stand_down(hass: HomeAssistant, hass_storage, services, freezer) -> None:
    today = dt_util.start_of_local_day()
    await tick(hass, freezer, local(today, 10, 0))
    seed_store(hass_storage, t_rm=10.0, settings={"schedule": True, **LEARN})
    human(hass, preset="eco", current=18.0, target=15.0)
    set_numbers(hass)
    room = await setup_room(hass, make_entry(options=opts()))
    freezer.move_to(local(today, 10, 10))
    human(hass, preset="comfort", current=18.0, target=20.5)
    await hass.async_block_till_done()
    assert room.standdown_active
    await toggle(hass, PAUSED, True)
    await toggle(hass, PAUSED, False)
    assert not room.standdown_active
    assert services["set_preset"][-1].data["preset_mode"] == "eco"


async def test_unpause_with_the_schedule_switch_off_changes_no_preset(hass: HomeAssistant, hass_storage, services, freezer) -> None:
    today = dt_util.start_of_local_day()
    await tick(hass, freezer, local(today, 10, 0))
    seed_store(hass_storage, t_rm=10.0)
    set_vtherm(hass, preset="comfort", current=18.0)
    set_numbers(hass)
    await setup_room(hass, make_entry(options=opts()))
    await toggle(hass, PAUSED, True)
    await toggle(hass, PAUSED, False)
    assert services["set_preset"] == []


async def test_unpause_while_a_window_is_open_waits_for_it(hass: HomeAssistant, hass_storage, services, freezer) -> None:
    today = dt_util.start_of_local_day()
    await tick(hass, freezer, local(today, 10, 0))
    seed_store(hass_storage, t_rm=10.0, settings={"schedule": True})
    set_vtherm(hass, preset="eco", current=18.0)
    set_numbers(hass)
    room = await setup_room(hass, make_entry(options=opts()))
    await toggle(hass, PAUSED, True)
    automation(hass, preset="comfort", current=18.0, window="on")
    await hass.async_block_till_done()
    await toggle(hass, PAUSED, False)
    assert room.dormant_reason == "window" and services["set_preset"] == []
    automation(hass, preset="comfort", current=18.0, window="off")
    await hass.async_block_till_done()
    assert services["set_preset"][-1].data["preset_mode"] == "eco"  # the pause ended earlier: the fresh start happens now


async def test_leaving_other_dormancy_still_lets_a_manual_change_stand(hass: HomeAssistant, hass_storage, services, freezer) -> None:
    """Only the pause is a fresh start: a window closing does not undo what somebody set meanwhile (unchanged behaviour)."""
    today = dt_util.start_of_local_day()
    await tick(hass, freezer, local(today, 10, 0))
    seed_store(hass_storage, t_rm=10.0, settings={"schedule": True})
    set_vtherm(hass, preset="eco", current=18.0)
    set_numbers(hass)
    room = await setup_room(hass, make_entry(options=opts()))
    freezer.move_to(local(today, 10, 5))
    human(hass, preset="comfort", current=18.0, window="on")
    await hass.async_block_till_done()
    assert room.dormant_reason == "window"
    human(hass, preset="comfort", current=18.0, window="off")
    await hass.async_block_till_done()
    assert services["set_preset"] == []


# ---------------------------------------------------------------- human or automation


async def test_a_persons_change_stands_down_and_is_learnt(hass: HomeAssistant, hass_storage, services, freezer) -> None:
    today = dt_util.start_of_local_day()
    await tick(hass, freezer, local(today, 15, 0))
    seed_store(hass_storage, t_rm=14.0, settings=LEARN, extra={"last_written": {"comfort": 21.0}})
    human(hass, preset="comfort", current=19.0, target=21.0)
    set_numbers(hass, comfort=21.0)
    room = await setup_room(hass, make_entry())
    human(hass, preset="boost", current=19.0, target=22.0)
    await hass.async_block_till_done()
    assert room.standdown_active and room.standdown.cause == "manual_preset"
    assert len(room.ledger.entries) == 1
    assert room.last_external_change["origin"] == "human"
    assert hass.states.get(STANDDOWN).attributes["last_origin"] == "human"


async def test_an_automations_change_stands_down_but_is_not_learnt(hass: HomeAssistant, hass_storage, services, freezer) -> None:
    today = dt_util.start_of_local_day()
    await tick(hass, freezer, local(today, 15, 0))
    seed_store(hass_storage, t_rm=14.0, settings=LEARN, extra={"last_written": {"comfort": 21.0}})
    automation(hass, preset="comfort", current=19.0, target=21.0)
    set_numbers(hass, comfort=21.0)
    room = await setup_room(hass, make_entry())
    automation(hass, preset="boost", current=19.0, target=22.0)
    await hass.async_block_till_done()
    assert room.standdown_active and room.standdown.cause == "automation_preset"
    assert room.standdown.until == local(today, 18, 0)  # the same stand-down rules: no schedule boundary, 180 min timeout
    assert room.ledger.entries == []
    assert room.learning["ignored"][-1]["reason"] == "automation" and room.learning["ignored"][-1]["kind"] == "preset"
    assert "last_override" not in room.learning
    attrs = hass.states.get(STANDDOWN).attributes
    assert attrs["last_origin"] == "automation" and attrs["cause"] == "automation_preset"
    assert room.last_external_change["origin"] == "automation"
    # Hearth does not fight it while stood down
    await room.async_set_setting("adaptive_slope", 0.3)
    assert services["set_value"] == []


async def test_an_automations_temperature_change_is_not_learnt_either(hass: HomeAssistant, hass_storage, services, freezer) -> None:
    today = dt_util.start_of_local_day()
    await tick(hass, freezer, local(today, 15, 0))
    seed_store(hass_storage, t_rm=14.0, settings=LEARN, extra={"last_written": {"comfort": 21.0}})
    automation(hass, preset="comfort", current=19.0, target=21.0)
    set_numbers(hass, comfort=21.0)
    room = await setup_room(hass, make_entry())
    automation(hass, preset="comfort", current=19.0, target=19.0)  # an away temperature cap
    await hass.async_block_till_done()
    assert room.standdown_active and room.standdown.cause == "automation_temp"
    assert room.ledger.entries == [] and room.learning["ignored"][-1]["reason"] == "automation"


async def test_learn_from_automations_restores_the_old_behaviour(hass: HomeAssistant, hass_storage, services, freezer) -> None:
    today = dt_util.start_of_local_day()
    await tick(hass, freezer, local(today, 15, 0))
    seed_store(hass_storage, t_rm=14.0, settings=LEARN, extra={"last_written": {"comfort": 21.0}})
    automation(hass, preset="comfort", current=19.0, target=21.0)
    set_numbers(hass, comfort=21.0)
    room = await setup_room(hass, make_entry())
    hass.data[DOMAIN]["defaults"]["learn_from_automations"] = True
    automation(hass, preset="boost", current=19.0, target=22.0)
    await hass.async_block_till_done()
    assert room.standdown.cause == "manual_preset"
    assert len(room.ledger.entries) == 1


async def test_a_persons_number_edit_counts_as_a_person(hass: HomeAssistant, hass_storage, services, freezer) -> None:
    today = dt_util.start_of_local_day()
    await tick(hass, freezer, local(today, 15, 0))
    seed_store(hass_storage, t_rm=14.0, settings=LEARN, extra={"last_written": {"comfort": 21.0}})
    automation(hass, preset="comfort", current=19.0, target=21.0)
    set_numbers(hass, comfort=21.0)
    room = await setup_room(hass, make_entry())
    freezer.tick(60)
    # The person edits the preset number; VTherm then moves its own target without their context
    hass.states.async_set(COMFORT_NUMBER, "20.0", {"min": 5, "max": 30, "step": 0.5}, context=Context(user_id="ben"))
    automation(hass, preset="comfort", current=19.0, target=20.0)
    await hass.async_block_till_done()
    assert room.standdown.cause == "manual_temp"
    assert room.ledger.entries and room.ledger.entries[-1].direction == -1


async def test_hearths_own_writes_are_neither(hass: HomeAssistant, hass_storage, services, freezer) -> None:
    today = dt_util.start_of_local_day()
    await tick(hass, freezer, local(today, 5, 0))
    seed_store(hass_storage, t_rm=10.0, settings={"schedule": True, **LEARN})
    automation(hass, preset="eco", current=18.0, target=15.0)
    set_numbers(hass)
    room = await setup_room(hass, make_entry(options=opts()))
    await tick(hass, freezer, local(today, 6, 30, 3))
    assert services["set_preset"][-1].data["preset_mode"] == "comfort"
    # VTherm reflects Hearth's switch, whichever context the state change carries
    automation(hass, preset="comfort", current=18.0, target=20.5)
    await hass.async_block_till_done()
    assert not room.standdown_active and room.ledger.entries == [] and room.learning["ignored"] == []
    await tick(hass, freezer, local(today, 9, 0, 3))
    assert services["set_preset"][-1].data["preset_mode"] == "eco"
    human(hass, preset="eco", current=18.0, target=15.0)
    await hass.async_block_till_done()
    assert not room.standdown_active and room.ledger.entries == [] and room.learning["ignored"] == []
