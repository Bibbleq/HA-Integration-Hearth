"""Phase 4: override detection, stand-down and the learning ledger through HA."""

from __future__ import annotations

from datetime import timedelta

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.util import dt as dt_util

from custom_components.hearth.const import DOMAIN

from .conftest import COMFORT_NUMBER, VTHERM, WEATHER, make_entry, seed_store, set_numbers, set_vtherm, setup_room
from .test_forecast_skip import install_weather, local, refresh_forecast
from .test_schedule import SCHEDULE, tick

LEARN = {"learning": True}


async def test_manual_preset_change_stands_down_and_records(hass: HomeAssistant, hass_storage, services, freezer) -> None:
    today = dt_util.start_of_local_day()
    await tick(hass, freezer, local(today, 15, 0))
    seed_store(hass_storage, t_rm=14.0, settings=LEARN, extra={"last_written": {"comfort": 21.0}})
    set_vtherm(hass, preset="comfort", current=19.0, target=21.0)
    set_numbers(hass, comfort=21.0)
    room = await setup_room(hass, make_entry(options={"weather_entity_id": WEATHER}))
    assert hass.states.get("sensor.hearth_living_room_stand_down").state == "none"
    # Someone switches to boost (22.0): a +1.0 C "turn up" in comfort
    set_vtherm(hass, preset="boost", current=19.0, target=22.0)
    await hass.async_block_till_done()
    assert room.standdown_active
    assert room.standdown.cause == "manual_preset"
    assert room.standdown.until == local(today, 18, 0)  # 180 min timeout, no schedule boundary
    sd = hass.states.get("sensor.hearth_living_room_stand_down")
    assert sd.state == local(today, 18, 0).isoformat()
    assert sd.attributes["cause"] == "manual_preset"
    assert len(room.ledger.entries) == 1
    entry = room.ledger.entries[0]
    assert entry.direction == 1 and entry.magnitude == 1.0 and entry.kind == "preset"
    assert entry.bucket == "baseline_error"
    assert entry.t_rm == 14.0
    # Adaptive is held while standing down even if the target moves
    await room.async_set_setting("adaptive_slope", 0.3)
    assert services["set_value"] == []
    # Stand-down ends -> adaptive writes again
    await tick(hass, freezer, local(today, 18, 1))
    assert not room.standdown_active
    assert services["set_value"][-1].data["value"] == 21.5
    assert float(hass.states.get("sensor.hearth_living_room_learning_baseline_error").state) == 1.0


async def test_temp_edit_records_and_our_own_writes_do_not(hass: HomeAssistant, hass_storage, services, freezer) -> None:
    today = dt_util.start_of_local_day()
    await tick(hass, freezer, local(today, 15, 0))
    seed_store(hass_storage, t_rm=14.0, settings=LEARN)
    set_vtherm(hass, preset="comfort", current=19.0, target=20.5)
    set_numbers(hass, comfort=20.5)
    room = await setup_room(hass, make_entry(options={"weather_entity_id": WEATHER}))
    # Our adaptive write (21.0) is reflected by VTherm: not an override
    assert services["set_value"][-1].data["value"] == 21.0
    set_numbers(hass, comfort=21.0)
    set_vtherm(hass, preset="comfort", current=19.0, target=21.0)
    await hass.async_block_till_done()
    assert not room.standdown_active and room.ledger.entries == []
    # A person edits the comfort number down to 20.0 (VTherm target follows)
    freezer.tick(timedelta(minutes=10))
    set_numbers(hass, comfort=20.0)
    set_vtherm(hass, preset="comfort", current=19.0, target=20.0)
    await hass.async_block_till_done()
    assert room.standdown_active and room.standdown.cause == "manual_temp"
    assert room.ledger.entries[-1].direction == -1 and room.ledger.entries[-1].magnitude == 1.0


async def test_learning_switch_off_records_nothing(hass: HomeAssistant, hass_storage, services, freezer) -> None:
    today = dt_util.start_of_local_day()
    await tick(hass, freezer, local(today, 15, 0))
    seed_store(hass_storage, t_rm=14.0)
    set_vtherm(hass, preset="comfort", current=19.0, target=20.5)
    set_numbers(hass)
    room = await setup_room(hass, make_entry(options={"weather_entity_id": WEATHER}))
    set_vtherm(hass, preset="boost", current=19.0, target=22.0)
    await hass.async_block_till_done()
    assert not room.standdown_active
    assert room.ledger.entries == []
    assert room.last_external_change["to"] == "boost"  # still noted for the schedule and skip logic


async def test_exclusions_dormant_storm_unaffected(hass: HomeAssistant, hass_storage, services, freezer) -> None:
    today = dt_util.start_of_local_day()
    await tick(hass, freezer, local(today, 15, 0))
    seed_store(hass_storage, t_rm=14.0, settings=LEARN)
    set_vtherm(hass, preset="comfort", current=19.0, target=20.5, window="on")
    set_numbers(hass)
    room = await setup_room(hass, make_entry(options={"weather_entity_id": WEATHER}))
    set_vtherm(hass, preset="boost", current=19.0, target=22.0, window="on")
    await hass.async_block_till_done()
    assert room.ledger.entries == []
    assert room.learning["ignored"][-1]["reason"] == "dormant"
    # Window closes, still boost: no override event
    set_vtherm(hass, preset="boost", current=19.0, target=22.0)
    await hass.async_block_till_done()
    assert room.ledger.entries == [] and not room.standdown_active
    # boost -> comfort qualifies (switching into an affected preset, cooler)
    freezer.tick(timedelta(hours=4))
    set_vtherm(hass, preset="comfort", current=19.0, target=20.5)
    await hass.async_block_till_done()
    assert len(room.ledger.entries) == 1 and room.ledger.entries[-1].direction == -1
    # comfort -> eco qualifies (turned down from an affected preset)
    freezer.tick(timedelta(hours=4))
    set_vtherm(hass, preset="eco", current=19.0, target=15.0)
    await hass.async_block_till_done()
    assert len(room.ledger.entries) == 2 and room.ledger.entries[-1].direction == -1
    # Eco temperature edited by hand: unaffected preset
    freezer.tick(timedelta(hours=4))
    set_vtherm(hass, preset="eco", current=19.0, target=16.0)
    await hass.async_block_till_done()
    assert len(room.ledger.entries) == 2
    assert room.learning["ignored"][-1]["reason"] == "unaffected_preset"
    assert not room.standdown_active
    # eco -> frost makes the room dormant: ignored as such
    set_vtherm(hass, preset="frost", current=19.0, target=7.0)
    await hass.async_block_till_done()
    assert room.learning["ignored"][-1]["reason"] == "dormant"
    # frost -> comfort qualifies (warmer)
    freezer.tick(timedelta(hours=4))
    set_vtherm(hass, preset="comfort", current=19.0, target=20.5)
    await hass.async_block_till_done()
    assert len(room.ledger.entries) == 3 and room.ledger.entries[-1].direction == 1
    # Storm: another change 30 min later stands the room down again but is not recorded
    freezer.tick(timedelta(minutes=30))
    set_vtherm(hass, preset="boost", current=19.0, target=22.0)
    await hass.async_block_till_done()
    assert len(room.ledger.entries) == 3
    assert room.learning["ignored"][-1]["reason"] == "storm"
    assert room.standdown_active


async def test_attribution_skip_failure_and_preheat(hass: HomeAssistant, hass_storage, services, freezer) -> None:
    today = dt_util.start_of_local_day()
    await tick(hass, freezer, local(today, 6, 0))
    seed_store(hass_storage, t_rm=10.0, settings={**LEARN, "skip": True, "schedule": True})
    set_vtherm(hass, preset="eco", current=18.0, target=15.0)
    set_numbers(hass)
    install_weather(hass, high=25.0)
    room = await setup_room(hass, make_entry(options={"weather_entity_id": WEATHER, "schedule": SCHEDULE, "skip_decision_time": "06:00"}))
    await refresh_forecast(hass)
    assert room.skip["status"] == "active"
    # 07:00 during the skip someone turns it to comfort: skip failure, weight 2
    await tick(hass, freezer, local(today, 7, 0))
    set_vtherm(hass, preset="comfort", current=18.0, target=20.5)
    await hass.async_block_till_done()
    assert room.skip["status"] == "aborted" and room.skip["aborted_reason"] == "manual_intervention"
    e = room.ledger.entries[-1]
    assert e.bucket == "skip_failure" and e.weight == 2.0 and e.direction == 1
    assert room.standdown_active
    # Stand-down ends at the next block boundary (09:00), sooner than the timeout
    assert room.standdown.until == local(today, 9, 0)
    assert hass.states.get("sensor.hearth_living_room_learning_skip_failure").attributes["events"] == 1
    # 09:00 eco block applies once the stand-down has ended
    await tick(hass, freezer, local(today, 9, 0, 3))
    assert services["set_preset"][-1].data["preset_mode"] == "eco"
    set_vtherm(hass, preset="eco", current=18.0, target=15.0)
    await hass.async_block_till_done()
    # 17:30, 30 min after the 17:00 warm-by: preheat shortfall
    await tick(hass, freezer, local(today, 17, 0, 3))
    assert services["set_preset"][-1].data["preset_mode"] == "comfort"
    set_vtherm(hass, preset="comfort", current=19.0, target=20.5)
    await hass.async_block_till_done()
    await tick(hass, freezer, local(today, 17, 30))
    set_vtherm(hass, preset="boost", current=19.0, target=22.0)
    await hass.async_block_till_done()
    assert room.ledger.entries[-1].bucket == "preheat_shortfall"


async def test_slope_bucket_when_cold(hass: HomeAssistant, hass_storage, services, freezer) -> None:
    today = dt_util.start_of_local_day()
    await tick(hass, freezer, local(today, 15, 0))
    seed_store(hass_storage, t_rm=2.0, settings=LEARN)
    set_vtherm(hass, preset="comfort", current=19.0, target=20.5)
    set_numbers(hass)
    room = await setup_room(hass, make_entry(options={"weather_entity_id": WEATHER}))
    services["set_value"].clear()
    set_vtherm(hass, preset="boost", current=19.0, target=22.0)
    await hass.async_block_till_done()
    assert room.ledger.entries[-1].bucket == "slope_error"


async def test_apply_learning_needs_evidence_and_is_bounded(hass: HomeAssistant, hass_storage, services, freezer) -> None:
    today = dt_util.start_of_local_day()
    await tick(hass, freezer, local(today, 15, 0))
    now = dt_util.utcnow()
    stats = {"mean": 3.0, "weight": 4.0, "pos": 4.0, "neg": 0.0, "events": 4, "last_at": now.isoformat()}
    ledger = {"entries": [], "buckets": {"baseline_error": stats}, "max_entries": 200}
    seed_store(hass_storage, t_rm=10.0, settings={**LEARN, "apply_learning": True}, extra={"mechanisms": {"ledger": ledger}})
    set_vtherm(hass, preset="comfort", current=19.0, target=20.5)
    set_numbers(hass, comfort=20.5)
    room = await setup_room(hass, make_entry(options={"weather_entity_id": WEATHER}))
    # Correction bounded to +1.0 -> target 21.5 (T_rm at reference, no adaptive offset)
    assert room.learned_target_offset() == 1.0
    assert services["set_value"][-1].data["value"] == 21.5
    set_numbers(hass, comfort=21.5)
    assert hass.states.get("sensor.hearth_living_room_learning_baseline_error").attributes["would_apply"] == 1.0
    # Switch application off -> back to 20.5
    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": "switch.hearth_living_room_apply_learned_corrections"}, blocking=True
    )
    await hass.async_block_till_done()
    assert services["set_value"][-1].data["value"] == 20.5
    # Too little evidence -> nothing applied
    room.ledger.buckets["baseline_error"].pos = 2.0
    await room.async_set_setting("apply_learning", True)
    assert room.learned_target_offset() == 0.0


async def test_apply_learning_never_escapes_band(hass: HomeAssistant, hass_storage, services, freezer) -> None:
    today = dt_util.start_of_local_day()
    await tick(hass, freezer, local(today, 15, 0))
    now = dt_util.utcnow()
    stats = {"mean": 1.0, "weight": 4.0, "pos": 4.0, "neg": 0.0, "events": 4, "last_at": now.isoformat()}
    ledger = {"entries": [], "buckets": {"baseline_error": stats, "slope_error": dict(stats)}, "max_entries": 200}
    seed_store(hass_storage, t_rm=40.0, settings={**LEARN, "apply_learning": True}, extra={"mechanisms": {"ledger": ledger}})
    set_vtherm(hass, preset="comfort", current=19.0, target=20.5)
    set_numbers(hass, comfort=20.5)
    await setup_room(hass, make_entry(options={"weather_entity_id": WEATHER, "band_max": 21.5}))
    assert services["set_value"][-1].data["value"] == 21.5
    assert all(c.data["value"] <= 21.5 for c in services["set_value"])


async def test_apply_learning_skip_threshold_and_safety_factor(hass: HomeAssistant, hass_storage, services, freezer) -> None:
    today = dt_util.start_of_local_day()
    await tick(hass, freezer, local(today, 4, 0))
    now = dt_util.utcnow()
    skip_stats = {"mean": 0.8, "weight": 3.0, "pos": 3.0, "neg": 0.0, "events": 3, "last_at": now.isoformat()}
    preheat_stats = {"mean": -2.0, "weight": 3.0, "pos": 0.0, "neg": 3.0, "events": 3, "last_at": now.isoformat()}
    ledger = {"entries": [], "buckets": {"skip_failure": skip_stats, "preheat_shortfall": preheat_stats}, "max_entries": 200}
    seed_store(
        hass_storage,
        t_rm=10.0,
        settings={**LEARN, "apply_learning": True, "schedule": True, "preheat": True},
        extra={"mechanisms": {"ledger": ledger}},
    )
    set_vtherm(hass, preset="eco", current=19.0, target=15.0, outdoor=5.0)
    set_numbers(hass, comfort=20.5)
    room = await setup_room(hass, make_entry(options={"weather_entity_id": WEATHER, "schedule": SCHEDULE}))
    assert room.effective_skip_threshold() == pytest.approx(18.8)
    assert room.learned_safety_factor_multiplier() == pytest.approx(0.75)
    # lead = 1.5 / 1.0 * 60 * 1.15 * 0.75 = 77.6 min
    assert room.preheat_plan.lead_min == pytest.approx(1.5 * 60 * 1.15 * 0.75)


async def test_override_service_and_reset_learning(hass: HomeAssistant, hass_storage, services, freezer) -> None:
    today = dt_util.start_of_local_day()
    await tick(hass, freezer, local(today, 6, 50))
    seed_store(hass_storage, t_rm=14.0, settings={**LEARN, "skip": True})
    set_vtherm(hass, preset="comfort", current=18.0, target=20.5)
    set_numbers(hass)
    install_weather(hass, high=25.0)
    room = await setup_room(hass, make_entry(options={"weather_entity_id": WEATHER}))
    await refresh_forecast(hass)
    assert room.skip["status"] == "active"
    set_vtherm(hass, preset="eco", current=18.0, target=15.0)
    await hass.async_block_till_done()
    # Deliberate override for 2 h: skip ends with restore, stand-down until 08:50
    await hass.services.async_call(DOMAIN, "override", {"vtherm_entity_id": VTHERM, "duration": {"hours": 2}}, blocking=True)
    await hass.async_block_till_done()
    assert room.skip["status"] == "idle" and room.skip["end_reason"] == "override_service"
    assert services["set_preset"][-1].data["preset_mode"] == "comfort"
    assert room.standdown.cause == "service"
    assert room.standdown.until == local(today, 8, 50)
    # Our restore write is not an override
    set_vtherm(hass, preset="comfort", current=18.0, target=20.5)
    await hass.async_block_till_done()
    assert room.ledger.entries == []
    # reset_learning validates the bucket
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, "reset_learning", {"vtherm_entity_id": VTHERM, "bucket": "nope"}, blocking=True)
    room.ledger.buckets["baseline_error"].events = 5
    await hass.services.async_call(DOMAIN, "reset_learning", {"vtherm_entity_id": VTHERM, "bucket": "baseline_error"}, blocking=True)
    await hass.async_block_till_done()
    assert room.ledger.buckets["baseline_error"].events == 0
    room.ledger.buckets["skip_failure"].events = 5
    await hass.services.async_call(DOMAIN, "reset_learning", {"vtherm_entity_id": VTHERM}, blocking=True)
    await hass.async_block_till_done()
    assert all(b.events == 0 for b in room.ledger.buckets.values())


async def test_standdown_survives_restart(hass: HomeAssistant, hass_storage, services, freezer) -> None:
    today = dt_util.start_of_local_day()
    await tick(hass, freezer, local(today, 15, 0))
    until = local(today, 17, 0)
    seed_store(
        hass_storage,
        t_rm=14.0,
        settings=LEARN,
        extra={
            "mechanisms": {
                "standdown": {"until": until.isoformat(), "cause": "manual_preset", "started_at": local(today, 14, 0).isoformat()}
            }
        },
    )
    set_vtherm(hass, preset="comfort", current=19.0, target=20.5)
    set_numbers(hass, comfort=20.5)
    room = await setup_room(hass, make_entry(options={"weather_entity_id": WEATHER}))
    assert room.standdown_active
    assert services["set_value"] == []
    await tick(hass, freezer, local(today, 17, 1))
    assert services["set_value"][-1].data == {"entity_id": COMFORT_NUMBER, "value": 21.0}


async def test_skip_floor_abort_still_applies_during_standdown(hass: HomeAssistant, hass_storage, services, freezer) -> None:
    today = dt_util.start_of_local_day()
    await tick(hass, freezer, local(today, 6, 50))
    seed_store(hass_storage, t_rm=14.0, settings={**LEARN, "skip": True})
    set_vtherm(hass, preset="comfort", current=18.0, target=20.5)
    set_numbers(hass)
    install_weather(hass, high=25.0)
    room = await setup_room(hass, make_entry(options={"weather_entity_id": WEATHER}))
    await refresh_forecast(hass)
    set_vtherm(hass, preset="eco", current=18.0, target=15.0)
    await hass.async_block_till_done()
    room._start_standdown(dt_util.utcnow(), "test")
    set_vtherm(hass, preset="eco", current=15.0, target=15.0)
    await hass.async_block_till_done()
    assert room.skip["status"] == "aborted" and room.skip["aborted_reason"] == "below_indoor_floor"
    assert services["set_preset"][-1].data["preset_mode"] == "comfort"
