"""0.7.5: a VTherm reload neither ends a preheat nor reads as an override (D66); a person's stepped target change is one
override (D67); the warming rate uses the evidence it has before the five-run fit (D68)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from .conftest import VTHERM, WEATHER, make_entry, seed_store, set_numbers, set_vtherm, setup_room
from .test_forecast_skip import local
from .test_preheat_latch import _preheating_room, presets
from .test_schedule import opts, tick
from .test_startup import initialising

RATE = "sensor.hearth_living_room_warming_rate"


def diagnostics(hass: HomeAssistant) -> list[dict]:
    events: list[dict] = []
    hass.bus.async_listen("hearth_diagnostic", lambda e: events.append(e.data))
    return events


# ---------------------------------------------------------------- D66: a VTherm reload mid-preheat


async def _reload(hass: HomeAssistant, freezer, room, *, current: float, target: float) -> None:
    """What a VTherm looks like through a config-entry reload: unavailable, then off and not ready, then its restored state."""
    hass.states.async_set(VTHERM, "unavailable", {})
    await hass.async_block_till_done()
    assert room.dormant_reason == "unavailable"
    freezer.tick(timedelta(milliseconds=300))
    initialising(hass)
    await hass.async_block_till_done()
    assert room.dormant_reason == "unavailable"  # not ready is as good as unavailable
    freezer.tick(timedelta(milliseconds=300))
    set_vtherm(hass, preset="comfort", current=current, target=target, outdoor=5.0, hvac_action="heating")  # the restore, no user
    await hass.async_block_till_done()
    await hass.async_block_till_done()
    assert not room.dormant


async def test_a_vtherm_reload_mid_preheat_keeps_the_latch_and_the_run(hass: HomeAssistant, hass_storage, services, freezer) -> None:
    """Main Bedroom, 25 Oct 18:46: a central-config change reloaded every VTherm mid-preheat. The latch ended on
    `unavailable`, Hearth wrote eco the moment the VTherm was back, the VTherm put comfort back and that read as an
    automation override until the 20:15 block. Now the latch holds, nothing is written, and the warm-up carries on."""
    today, room = await _preheating_room(hass, hass_storage, services, freezer)  # Hearth wrote comfort at 04:47
    freezer.move_to(local(today, 5, 10))
    set_vtherm(hass, preset="comfort", current=19.6, target=20.5, outdoor=5.0, hvac_action="heating")
    await hass.async_block_till_done()
    assert room.heating_run is not None and room.preheat_latched
    await _reload(hass, freezer, room, current=19.6, target=20.5)
    assert room.preheat_latched
    assert presets(services) == ["comfort"], "an eco write during the reload"
    assert not room.standdown_active
    assert room.heating_run is not None and not room.heating_run.tainted
    assert room.learning["ignored"][-1]["reason"] == "startup"
    # The VTherm is still sorting itself out for a moment after it is ready: a target wobble with no user behind it is
    # a restore too (0.9 s after ready on 25 Oct)
    freezer.tick(timedelta(seconds=1))
    set_vtherm(hass, preset="comfort", current=19.6, target=21.0, outdoor=5.0, hvac_action="heating")
    await hass.async_block_till_done()
    assert not room.standdown_active and room.learning["ignored"][-1]["reason"] == "startup"
    assert room.preheat_latched
    freezer.tick(timedelta(seconds=1))
    set_vtherm(hass, preset="comfort", current=19.6, target=20.5, outdoor=5.0, hvac_action="heating")
    await hass.async_block_till_done()
    # The warm-up reaches target and counts; Hearth never wrote eco
    await tick(hass, freezer, local(today, 5, 30))  # the poll that picks the run up again after the wobble
    freezer.move_to(local(today, 6, 0))
    set_vtherm(hass, preset="comfort", current=20.5, target=20.5, outdoor=5.0, hvac_action="idle")
    await hass.async_block_till_done()
    assert room.warming_model.n_runs == 1
    assert "eco" not in presets(services)


async def test_a_person_still_counts_during_the_reload_grace(hass: HomeAssistant, hass_storage, services, freezer) -> None:
    today, room = await _preheating_room(hass, hass_storage, services, freezer)
    freezer.move_to(local(today, 5, 10))
    await _reload(hass, freezer, room, current=19.6, target=20.5)
    freezer.tick(timedelta(seconds=5))
    set_vtherm(hass, preset="eco", current=19.6, target=15.0, outdoor=5.0, user_id="ben")
    await hass.async_block_till_done()
    assert room.standdown_active and room.standdown.cause == "manual_preset"
    assert not room.preheat_latched


async def test_an_automation_change_after_the_reload_grace_stands_down_as_before(
    hass: HomeAssistant, hass_storage, services, freezer
) -> None:
    today, room = await _preheating_room(hass, hass_storage, services, freezer)
    freezer.move_to(local(today, 5, 10))
    await _reload(hass, freezer, room, current=19.6, target=20.5)
    freezer.tick(timedelta(seconds=61))
    set_vtherm(hass, preset="eco", current=19.6, target=15.0, outdoor=5.0)
    await hass.async_block_till_done()
    assert room.standdown_active and room.standdown.cause == "automation_preset"


async def test_other_dormancy_still_ends_the_latch(hass: HomeAssistant, hass_storage, services, freezer) -> None:
    """Only the VTherm blinking is transient: going away (presence off) still lets go of the room."""
    today, room = await _preheating_room(hass, hass_storage, services, freezer)
    freezer.move_to(local(today, 5, 10))
    set_vtherm(hass, preset="comfort", current=19.6, target=20.5, outdoor=5.0, hvac_action="heating", presence="off")
    await hass.async_block_till_done()
    assert room.dormant_reason == "away" and not room.preheat_latched


# ---------------------------------------------------------------- D67: a stepped change is one override


async def _comfort_room(hass: HomeAssistant, hass_storage, freezer, *, current: float = 21.6):
    """A live room on comfort 21.0 (adaptive already written), warmer than its target, with a person about to react."""
    today = dt_util.start_of_local_day()
    await tick(hass, freezer, local(today, 9, 0))
    seed_store(hass_storage, t_rm=7.5, settings={"learning": True}, extra={"last_written": {"comfort": 21.0}})
    set_vtherm(hass, preset="comfort", current=current, target=21.0)
    set_numbers(hass, comfort=21.0)
    room = await setup_room(hass, make_entry(options={"weather_entity_id": WEATHER}))
    freezer.move_to(local(today, 9, 22))  # clear of any settle window from setup
    return today, room


async def _drag(
    hass: HomeAssistant, freezer, start: float, stop: float, *, step: float = 0.1, every_ms: int = 300, preset: str = "comfort"
) -> list[float]:
    """A person dragging a thermostat slider: one target write per step, a few hundred milliseconds apart."""
    steps = round(abs(stop - start) / step)
    sign = 1 if stop > start else -1
    targets = [round(start + sign * step * (i + 1), 1) for i in range(steps)]
    for target in targets:
        freezer.tick(timedelta(milliseconds=every_ms))
        set_vtherm(hass, preset=preset, current=21.6, target=target, user_id="ben")
        await hass.async_block_till_done()
    return targets


async def test_a_slider_drag_is_one_override_with_the_net_change(hass: HomeAssistant, hass_storage, services, freezer) -> None:
    """Office, 26 Oct 09:22: 21 -> 19 in 0.1 C steps over six seconds was learnt as -0.1 (then twenty storms) and restarted
    the stand-down twenty-one times. It is one -2.0 C baseline_error entry, one stand-down, and one summary line."""
    today, room = await _comfort_room(hass, hass_storage, freezer)
    events = diagnostics(hass)
    first_at = dt_util.utcnow() + timedelta(milliseconds=300)
    await _drag(hass, freezer, 21.0, 19.0)
    assert len(room.ledger.entries) == 1
    entry = room.ledger.entries[0]
    assert entry.direction == -1 and entry.magnitude == pytest.approx(2.0) and entry.kind == "temp"
    assert entry.bucket == "baseline_error" and entry.at == first_at
    stats = room.ledger.buckets["baseline_error"]
    assert (stats.mean, stats.weight, stats.pos, stats.neg, stats.events) == pytest.approx((-2.0, 1.0, 0.0, 1.0, 1))
    assert room.learning["last_override"]["raw_magnitude"] == pytest.approx(-2.0)
    assert room.learning["last_override"]["steps"] == 20
    assert room.last_external_change["from"] == 21.0 and room.last_external_change["to"] == 19.0
    assert room.last_external_change["magnitude"] == pytest.approx(-2.0)
    assert not [i for i in room.learning["ignored"] if i["reason"] == "storm"]
    # One stand-down, started by the first step and left alone by the rest
    assert room.standdown_active and room.standdown.cause == "manual_temp" and room.standdown.started_at == first_at
    assert len([e for e in events if e["kind"] == "standdown"]) == 1
    assert float(hass.states.get("sensor.hearth_living_room_learning_baseline_error").state) == pytest.approx(-2.0)
    # The steps stop: one summary line
    freezer.tick(timedelta(seconds=16))
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()
    overrides = [e["message"] for e in events if e["kind"] == "override"]
    assert overrides[0] == "Override recorded: temp -0.1 C -> baseline_error"
    assert overrides[-1].startswith("Override settled: temp -2.0 C -> baseline_error (20 steps over 6 s)")
    assert room._coalesce is None
    # A fresh change later is judged on its own (and is a storm, as before)
    freezer.tick(timedelta(seconds=10))
    set_vtherm(hass, preset="comfort", current=21.6, target=18.5, user_id="ben")
    await hass.async_block_till_done()
    assert len(room.ledger.entries) == 1 and room.ledger.entries[0].magnitude == pytest.approx(2.0)
    assert room.learning["ignored"][-1]["reason"] == "storm"


async def test_a_drag_back_to_where_it_started_says_nothing(hass: HomeAssistant, hass_storage, services, freezer) -> None:
    today, room = await _comfort_room(hass, hass_storage, freezer)
    await _drag(hass, freezer, 21.0, 20.5)
    assert len(room.ledger.entries) == 1
    await _drag(hass, freezer, 20.5, 21.0)
    assert room.ledger.entries == [] and "last_override" not in room.learning
    assert room.ledger.buckets["baseline_error"].events == 0
    assert room.standdown_active  # the person did touch the room; Hearth still keeps out of the way


async def test_a_drag_that_turns_round_lands_on_its_net_direction(hass: HomeAssistant, hass_storage, services, freezer) -> None:
    today, room = await _comfort_room(hass, hass_storage, freezer)
    await _drag(hass, freezer, 21.0, 20.5)
    await _drag(hass, freezer, 20.5, 22.0)
    assert len(room.ledger.entries) == 1
    entry = room.ledger.entries[0]
    assert entry.direction == 1 and entry.magnitude == pytest.approx(1.0)
    stats = room.ledger.buckets["baseline_error"]
    assert (stats.mean, stats.pos, stats.neg, stats.events) == pytest.approx((1.0, 1.0, 0.0, 1))


async def test_a_manual_setpoint_then_steps_fold_too(hass: HomeAssistant, hass_storage, services, freezer) -> None:
    """A VTherm that drops to preset none on a manual setpoint: the first step is a `setpoint` change out of comfort, the
    rest are `temp` changes on preset none, which alone would be 'unaffected_preset'. Folded, they are the one change."""
    today, room = await _comfort_room(hass, hass_storage, freezer)
    freezer.tick(timedelta(milliseconds=300))
    set_vtherm(hass, preset="none", current=21.6, target=20.9, user_id="ben")
    await hass.async_block_till_done()
    assert room.standdown.cause == "manual_setpoint" and len(room.ledger.entries) == 1
    await _drag(hass, freezer, 20.9, 19.0, preset="none")
    assert len(room.ledger.entries) == 1
    assert room.ledger.entries[0].kind == "setpoint" and room.ledger.entries[0].magnitude == pytest.approx(2.0)
    assert room.last_external_change["from"] == 21.0 and room.last_external_change["to"] == 19.0


async def test_steps_too_far_apart_are_separate_changes(hass: HomeAssistant, hass_storage, services, freezer) -> None:
    today, room = await _comfort_room(hass, hass_storage, freezer)
    await _drag(hass, freezer, 21.0, 20.9)
    freezer.tick(timedelta(seconds=20))
    set_vtherm(hass, preset="comfort", current=21.6, target=20.8, user_id="ben")
    await hass.async_block_till_done()
    assert room.ledger.entries[0].magnitude == pytest.approx(0.1)
    assert room.learning["ignored"][-1]["reason"] == "storm"


async def test_steps_inside_the_settle_window_stay_unlearnt(hass: HomeAssistant, hass_storage, services, freezer) -> None:
    """The settle window (D64) still wins: a drag right after Hearth's own write stands the room down and learns nothing."""
    today, room = await _comfort_room(hass, hass_storage, freezer)
    room.write_log.record(VTHERM, "comfort", dt_util.utcnow() - timedelta(minutes=2), "schedule")
    await _drag(hass, freezer, 21.0, 20.0)
    assert room.ledger.entries == [] and room.standdown_active
    assert room.learning["ignored"][-1]["reason"] == "settling_own_write"


# ---------------------------------------------------------------- D68: the warming rate before five runs


def _runs(*rates: float) -> dict:
    return {"warming": {"a": None, "b": None, "runs": [[12.0, r] for r in rates], "max_runs": 30}}


async def test_the_median_of_a_few_runs_drives_the_preheat_lead(hass: HomeAssistant, hass_storage, services, freezer) -> None:
    """Living Room, 26 Oct: three runs of 1.47, 1.21 and 2.55 C/h, yet the lead used the 1.0 C/h default (120 min for a
    2.2 C deficit). The median, 1.47, gives 105 min; the sensor says which basis is in use."""
    today = dt_util.start_of_local_day()
    await tick(hass, freezer, local(today, 3, 0))
    seed_store(
        hass_storage,
        t_rm=10.0,
        settings={"schedule": True, "preheat": True, "adaptive": False},
        extra={"mechanisms": _runs(1.47, 1.21, 2.55)},
    )
    set_vtherm(hass, preset="eco", current=18.3, target=15.0, outdoor=5.0)
    set_numbers(hass, comfort=20.5)
    room = await setup_room(hass, make_entry(options=opts()))
    plan = room.preheat_plan
    assert plan.rate == pytest.approx(1.47) and plan.learned
    assert plan.lead_min == pytest.approx(2.2 / 1.47 * 60 * 1.15)
    state = hass.states.get(RATE)
    assert float(state.state) == pytest.approx(1.47)
    assert state.attributes["basis"] == "median" and state.attributes["learned"] is True
    assert state.attributes["evidence_count"] == 3 and state.attributes["early_min_runs"] == 2


async def test_one_run_is_not_enough_and_five_use_the_fit(hass: HomeAssistant, hass_storage, services, freezer) -> None:
    today = dt_util.start_of_local_day()
    await tick(hass, freezer, local(today, 3, 0))
    seed_store(hass_storage, t_rm=10.0, settings={"schedule": True, "preheat": True, "adaptive": False}, extra={"mechanisms": _runs(7.0)})
    set_vtherm(hass, preset="eco", current=18.3, target=15.0, outdoor=5.0)
    set_numbers(hass, comfort=20.5)
    room = await setup_room(hass, make_entry(options=opts()))
    assert room.preheat_plan.rate == 1.0 and not room.preheat_plan.learned
    assert hass.states.get(RATE).attributes["basis"] == "default"
    for _ in range(4):
        room.warming_model.add_run(12.0, 2.0)
    await room.async_evaluate("test")
    await hass.async_block_till_done()
    # five runs: the fit, flat with no spread in delta, so the mean (3.0) rather than the median (2.0)
    assert room.preheat_plan.learned and room.preheat_plan.rate == pytest.approx(3.0)
    assert hass.states.get(RATE).attributes["basis"] == "fit"
