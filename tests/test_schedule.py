"""Phase 3: warm-by schedule, preheat and the warming-rate learner through HA."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.hearth.const import DOMAIN

from .conftest import VTHERM, WEATHER, make_entry, seed_store, set_numbers, set_vtherm, setup_room
from .test_forecast_skip import install_weather, local, refresh_forecast

WEEKDAY = [
    {"warm_by": "06:30", "preset": "comfort", "skippable": True},
    {"at": "09:00", "preset": "eco"},
    {"warm_by": "17:00", "preset": "comfort"},
    {"at": "22:30", "preset": "eco"},
]
SCHEDULE = {d: WEEKDAY for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")}


def opts(**extra):
    return {"weather_entity_id": WEATHER, "schedule": SCHEDULE, **extra}


async def tick(hass: HomeAssistant, freezer, at: datetime) -> None:
    freezer.move_to(at)
    async_fire_time_changed(hass, at)
    await hass.async_block_till_done()


async def test_schedule_switches_presets_at_block_times(hass: HomeAssistant, hass_storage, services, freezer) -> None:
    today = dt_util.start_of_local_day()
    await tick(hass, freezer, local(today, 5, 0))
    seed_store(hass_storage, t_rm=10.0, settings={"schedule": True})
    set_vtherm(hass, preset="eco", current=18.0)
    set_numbers(hass)
    room = await setup_room(hass, make_entry(options=opts()))
    # 05:00: current block is yesterday's 22:30 eco; VTherm already eco -> no write, block recorded
    assert services["set_preset"] == []
    assert room.sched["applied_key"].endswith("|eco")
    assert hass.states.get("sensor.hearth_living_room_next_block").state == "comfort by 06:30"
    # 06:30 -> comfort (exact one-shot timer)
    await tick(hass, freezer, local(today, 6, 30, 3))
    assert services["set_preset"][-1].data == {"entity_id": VTHERM, "preset_mode": "comfort"}
    set_vtherm(hass, preset="comfort", current=18.0)
    await hass.async_block_till_done()
    # 09:00 -> eco
    await tick(hass, freezer, local(today, 9, 0, 3))
    assert services["set_preset"][-1].data["preset_mode"] == "eco"
    assert len(services["set_preset"]) == 2


async def test_manual_change_stands_until_next_block(hass: HomeAssistant, hass_storage, services, freezer) -> None:
    today = dt_util.start_of_local_day()
    await tick(hass, freezer, local(today, 10, 0))
    seed_store(hass_storage, t_rm=10.0, settings={"schedule": True})
    set_vtherm(hass, preset="eco", current=18.0)
    set_numbers(hass)
    room = await setup_room(hass, make_entry(options=opts()))
    assert services["set_preset"] == []
    # 10:30: someone puts it to boost by hand
    freezer.move_to(local(today, 10, 30))
    set_vtherm(hass, preset="boost", current=18.0)
    await hass.async_block_till_done()
    assert room.last_external_change["to"] == "boost"
    # Ticks do not fight it
    await tick(hass, freezer, local(today, 12, 0))
    assert services["set_preset"] == []
    # 17:00 block applies as normal
    await tick(hass, freezer, local(today, 17, 0, 3))
    assert services["set_preset"][-1].data["preset_mode"] == "comfort"


async def test_schedule_off_means_no_switching(hass: HomeAssistant, hass_storage, services, freezer) -> None:
    today = dt_util.start_of_local_day()
    await tick(hass, freezer, local(today, 6, 0))
    seed_store(hass_storage, t_rm=10.0)
    set_vtherm(hass, preset="eco", current=18.0)
    set_numbers(hass)
    await setup_room(hass, make_entry(options=opts()))
    await tick(hass, freezer, local(today, 6, 30, 3))
    assert services["set_preset"] == []
    assert hass.states.get("sensor.hearth_living_room_next_block").attributes["schedule_enabled"] is False


async def test_preheat_starts_early_with_default_rate(hass: HomeAssistant, hass_storage, services, freezer) -> None:
    today = dt_util.start_of_local_day()
    await tick(hass, freezer, local(today, 4, 0))
    seed_store(hass_storage, t_rm=10.0, settings={"schedule": True, "preheat": True})
    # comfort 20.5, room at 19.0 -> deficit 1.5 at 1.0 C/h * 1.15 = 103.5 min -> start 04:46:30
    set_vtherm(hass, preset="eco", current=19.0, outdoor=5.0)
    set_numbers(hass, comfort=20.5)
    room = await setup_room(hass, make_entry(options=opts()))
    plan = room.preheat_plan
    assert plan is not None and plan.lead_min == pytest.approx(103.5)
    assert plan.start == local(today, 6, 30) - timedelta(minutes=103.5)
    assert not plan.learned
    state = hass.states.get("sensor.hearth_living_room_next_block")
    assert state.state == "comfort by 06:30, preheat est. 04:46"
    assert state.attributes["preheat_rate"] == 1.0
    await tick(hass, freezer, local(today, 4, 40))
    assert services["set_preset"] == []
    await tick(hass, freezer, local(today, 4, 47))
    assert services["set_preset"][-1].data["preset_mode"] == "comfort"
    assert room.sched["applied_by"] == "preheat"
    set_vtherm(hass, preset="comfort", current=19.0, outdoor=5.0)
    await hass.async_block_till_done()
    # The 06:30 block itself is then already applied: no second write
    await tick(hass, freezer, local(today, 6, 30, 3))
    assert len(services["set_preset"]) == 1


async def test_preheat_capped_and_no_deficit(hass: HomeAssistant, hass_storage, services, freezer) -> None:
    today = dt_util.start_of_local_day()
    await tick(hass, freezer, local(today, 4, 0))
    seed_store(hass_storage, t_rm=10.0, settings={"schedule": True, "preheat": True})
    set_vtherm(hass, preset="eco", current=15.0, outdoor=5.0)  # deficit 5.5 -> capped at 120 min
    set_numbers(hass, comfort=20.5)
    room = await setup_room(hass, make_entry(options=opts()))
    assert room.preheat_plan.lead_min == 120
    set_vtherm(hass, preset="eco", current=21.0, outdoor=5.0)  # already warm
    await hass.async_block_till_done()
    assert room.preheat_plan.lead_min == 0
    assert hass.states.get("sensor.hearth_living_room_next_block").state == "comfort by 06:30"


async def test_learner_records_clean_run_and_uses_it(hass: HomeAssistant, hass_storage, services, freezer) -> None:
    today = dt_util.start_of_local_day()
    await tick(hass, freezer, local(today, 6, 0))
    seed_store(hass_storage, t_rm=10.0, settings={"adaptive": False})
    set_numbers(hass, comfort=20.5)
    set_vtherm(hass, preset="comfort", current=17.0, target=20.5, outdoor=5.0, hvac_action="idle")
    room = await setup_room(hass, make_entry(options=opts()))
    assert room.heating_run is None
    for i in range(5):
        # Five clean runs: 1.5 C rise over 60 min -> 1.5 C/h, delta 15.5
        start = local(today, 6 + i * 2, 0)
        freezer.move_to(start)
        set_vtherm(hass, preset="comfort", current=17.0, target=20.5, outdoor=5.0, hvac_action="heating")
        await hass.async_block_till_done()
        assert room.heating_run is not None
        await tick(hass, freezer, start + timedelta(minutes=30))
        set_vtherm(hass, preset="comfort", current=17.75, target=20.5, outdoor=5.0, hvac_action="heating")
        await hass.async_block_till_done()
        freezer.move_to(start + timedelta(minutes=60))
        set_vtherm(hass, preset="comfort", current=18.5, target=20.5, outdoor=5.0, hvac_action="heating")
        await hass.async_block_till_done()
        set_vtherm(hass, preset="comfort", current=18.5, target=20.5, outdoor=5.0, hvac_action="idle")
        await hass.async_block_till_done()
        assert room.heating_run is None
        assert room.warming_model.n_runs == i + 1
    rate, learned = room.current_warming_rate()
    assert learned and rate == pytest.approx(1.5)
    state = hass.states.get("sensor.hearth_living_room_warming_rate")
    assert float(state.state) == pytest.approx(1.5)
    assert state.attributes["evidence_count"] == 5
    assert state.attributes["learned"] is True
    # Persisted
    await room.async_unload()
    assert hass_storage["hearth.room.climate_living_room"]["data"]["mechanisms"]["warming"]["runs"][0] == [15.5, 1.5]


async def test_learner_discards_tainted_and_short_runs(hass: HomeAssistant, hass_storage, services, freezer) -> None:
    today = dt_util.start_of_local_day()
    await tick(hass, freezer, local(today, 6, 0))
    seed_store(hass_storage, t_rm=10.0, settings={"adaptive": False})
    set_numbers(hass)
    set_vtherm(hass, preset="comfort", current=17.0, target=20.5, outdoor=5.0, hvac_action="heating")
    room = await setup_room(hass, make_entry(options=opts()))
    assert room.heating_run is not None
    # Window opens mid-run -> tainted and discarded
    freezer.move_to(local(today, 6, 30))
    set_vtherm(hass, preset="comfort", current=17.5, target=20.5, outdoor=5.0, hvac_action="heating", window="on")
    await hass.async_block_till_done()
    assert room.heating_run is None and room.warming_model.n_runs == 0
    freezer.move_to(local(today, 7, 0))
    set_vtherm(hass, preset="comfort", current=18.5, target=20.5, outdoor=5.0, hvac_action="idle")
    await hass.async_block_till_done()
    assert room.heating_run is None and room.warming_model.n_runs == 0
    # Short run -> discarded
    freezer.move_to(local(today, 8, 0))
    set_vtherm(hass, preset="comfort", current=17.0, target=20.5, outdoor=5.0, hvac_action="heating")
    await hass.async_block_till_done()
    freezer.move_to(local(today, 8, 10))
    set_vtherm(hass, preset="comfort", current=18.0, target=20.5, outdoor=5.0, hvac_action="idle")
    await hass.async_block_till_done()
    assert room.warming_model.n_runs == 0
    # Eco heating (unaffected preset) never starts a run
    set_vtherm(hass, preset="eco", current=15.0, target=15.0, outdoor=5.0, hvac_action="heating")
    await hass.async_block_till_done()
    assert room.heating_run is None


async def test_skip_defers_skippable_block_and_ends_on_non_skippable(hass: HomeAssistant, hass_storage, services, freezer) -> None:
    today = dt_util.start_of_local_day()
    await tick(hass, freezer, local(today, 6, 0))
    seed_store(hass_storage, t_rm=10.0, settings={"schedule": True, "skip": True})
    set_vtherm(hass, preset="eco", current=18.0)
    set_numbers(hass)
    install_weather(hass, high=25.0)
    room = await setup_room(hass, make_entry(options=opts(skip_decision_time="06:00", skip_end_time="18:00")))
    await refresh_forecast(hass)
    assert room.skip["status"] == "active"
    assert services["set_preset"] == []  # already eco
    # 06:30 skippable comfort block: skip wins
    await tick(hass, freezer, local(today, 6, 30, 3))
    assert services["set_preset"] == []
    await tick(hass, freezer, local(today, 9, 0, 3))
    assert services["set_preset"] == []
    # 17:00 non-skippable comfort block ends the skip and applies comfort
    await tick(hass, freezer, local(today, 17, 0, 3))
    assert room.skip["status"] == "idle" and room.skip["end_reason"] == "non_skippable_block"
    assert services["set_preset"][-1].data["preset_mode"] == "comfort"
    assert len(services["set_preset"]) == 1


async def test_skip_end_restores_schedule_preset(hass: HomeAssistant, hass_storage, services, freezer) -> None:
    today = dt_util.start_of_local_day()
    await tick(hass, freezer, local(today, 6, 0))
    seed_store(hass_storage, t_rm=10.0, settings={"schedule": True, "skip": True})
    set_vtherm(hass, preset="comfort", current=18.0)
    set_numbers(hass)
    install_weather(hass, high=25.0)
    weekend = {"sat": [{"warm_by": "08:30", "preset": "comfort", "skippable": True}, {"at": "22:00", "preset": "eco"}]}
    sched = {d: weekend["sat"] for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")}
    room = await setup_room(hass, make_entry(options=opts(schedule=sched, skip_decision_time="09:00", skip_end_time="12:00")))
    # 08:30 comfort applied by schedule (already comfort, no write)
    await tick(hass, freezer, local(today, 8, 30, 3))
    await refresh_forecast(hass)
    await tick(hass, freezer, local(today, 9, 0, 5))
    assert room.skip["status"] == "active"
    assert services["set_preset"][-1].data["preset_mode"] == "eco"
    set_vtherm(hass, preset="eco", current=18.0)
    await hass.async_block_till_done()
    # 12:00 skip ends: current block is the (skippable) comfort block -> restore comfort
    await tick(hass, freezer, local(today, 12, 0, 5))
    assert room.skip["status"] == "idle"
    assert services["set_preset"][-1].data["preset_mode"] == "comfort"


async def test_set_schedule_service_validates_and_reloads(hass: HomeAssistant, hass_storage, services, freezer) -> None:
    today = dt_util.start_of_local_day()
    await tick(hass, freezer, local(today, 10, 0))
    seed_store(hass_storage, t_rm=10.0)
    set_vtherm(hass, preset="eco", current=18.0)
    set_numbers(hass)
    entry = make_entry(options={"weather_entity_id": WEATHER})
    room = await setup_room(hass, entry)
    assert room.schedule_model.is_empty
    assert hass.states.get("sensor.hearth_living_room_next_block").state == "unknown"
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "set_schedule", {"vtherm_entity_id": VTHERM, "schedule": {"mon": [{"preset": "eco"}]}}, blocking=True
        )
    await hass.services.async_call(DOMAIN, "set_schedule", {"vtherm_entity_id": VTHERM, "schedule": {"Monday": WEEKDAY}}, blocking=True)
    await hass.async_block_till_done()
    assert entry.options["schedule"] == {
        "mon": [
            {"warm_by": "06:30", "preset": "comfort", "skippable": True},
            {"at": "09:00", "preset": "eco"},
            {"warm_by": "17:00", "preset": "comfort"},
            {"at": "22:30", "preset": "eco"},
        ]
    }
    room = hass.data[DOMAIN]["rooms"][entry.entry_id]
    assert not room.schedule_model.is_empty


async def test_restart_does_not_refire_applied_block(hass: HomeAssistant, hass_storage, services, freezer) -> None:
    today = dt_util.start_of_local_day()
    await tick(hass, freezer, local(today, 7, 0))
    key = f"{local(today, 6, 30).isoformat()}|comfort"
    seed_store(
        hass_storage,
        t_rm=10.0,
        settings={"schedule": True},
        extra={"mechanisms": {"schedule": {"applied_key": key, "applied_by": "schedule"}}},
    )
    set_vtherm(hass, preset="boost", current=18.0)  # user changed it after the block started
    set_numbers(hass)
    await setup_room(hass, make_entry(options=opts()))
    assert services["set_preset"] == []
