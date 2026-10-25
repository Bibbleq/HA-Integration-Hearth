"""0.7.4: preheat holds once started (D62), a boost hands over to a close warm-by block (D63), and Hearth does not learn
from changes made while the house is still settling after its own actions (D64)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .conftest import make_entry, seed_store, set_numbers, set_vtherm, setup_room
from .test_boost import boost, restored
from .test_forecast_skip import local
from .test_schedule import opts, tick

NEXT = "sensor.hearth_living_room_next_block"
ALL = {"schedule": True, "preheat": True, "learning": True, "adaptive": False}


def presets(services) -> list[str]:
    return [c.data["preset_mode"] for c in services["set_preset"]]


async def _preheating_room(hass: HomeAssistant, hass_storage, services, freezer):
    """Default rate 1.0 C/h, room 19.0 against comfort 20.5 for 06:30: preheat estimated to start at 04:46:30."""
    today = dt_util.start_of_local_day()
    await tick(hass, freezer, local(today, 4, 0))
    seed_store(hass_storage, t_rm=10.0, settings=ALL)
    set_vtherm(hass, preset="eco", current=19.0, target=15.0, outdoor=5.0)
    set_numbers(hass, comfort=20.5)
    room = await setup_room(hass, make_entry(options=opts()))
    assert room.preheat_plan.start == local(today, 6, 30) - timedelta(minutes=103.5)
    await tick(hass, freezer, local(today, 4, 47))
    assert presets(services) == ["comfort"]
    set_vtherm(hass, preset="comfort", current=19.0, target=20.5, outdoor=5.0, hvac_action="heating")
    await hass.async_block_till_done()
    return today, room


async def test_preheat_holds_comfort_when_the_estimate_moves_later(hass: HomeAssistant, hass_storage, services, freezer) -> None:
    """Acceptance 1: the room warms, so a fresh plan starts later than now; the latched preheat does not let go."""
    today, room = await _preheating_room(hass, hass_storage, services, freezer)
    assert room.preheat_latched
    assert hass.states.get(NEXT).state == "comfort by 06:30, preheating since 04:47"
    for minute, temp in ((5, 19.5), (20, 19.8), (35, 20.0), (50, 20.2)):
        # 05:35 at 20.0: deficit 0.5 -> 34.5 min -> a fresh estimate of 05:55:30, later than now
        freezer.move_to(local(today, 5, minute))
        set_vtherm(hass, preset="comfort", current=temp, target=20.5, outdoor=5.0, hvac_action="heating")
        await hass.async_block_till_done()
        await tick(hass, freezer, local(today, 5, minute, 30))
        assert presets(services) == ["comfort"], f"flapped at 05:{minute:02d}"
        if temp >= 20.0:  # the condition that used to drop the room back to eco
            assert room._plan_preheat(dt_util.utcnow()).start > dt_util.utcnow()
        assert room.preheat_plan.latched and room.preheat_plan.start == local(today, 4, 47)
        assert hass.states.get(NEXT).state == "comfort by 06:30, preheating since 04:47"
    # Reaches target early: stays on comfort (the VTherm idles), the run counts
    freezer.move_to(local(today, 6, 5))
    set_vtherm(hass, preset="comfort", current=20.5, target=20.5, outdoor=5.0, hvac_action="idle")
    await hass.async_block_till_done()
    assert room.warming_model.n_runs == 1
    delta, rate = room.warming_model.runs[0]
    assert rate == pytest.approx(1.5 / (78 / 60), rel=0.01)  # 19.0 -> 20.5 between 04:47 and 06:05
    await tick(hass, freezer, local(today, 6, 20))
    assert presets(services) == ["comfort"]
    # The block starts: the latch ends, no second write
    await tick(hass, freezer, local(today, 6, 30, 3))
    assert presets(services) == ["comfort"]
    assert not room.preheat_latched


async def test_latch_survives_a_restart(hass: HomeAssistant, hass_storage, services, freezer) -> None:
    today, room = await _preheating_room(hass, hass_storage, services, freezer)
    await room.async_unload()
    assert hass_storage["hearth.room.climate_living_room"]["data"]["mechanisms"]["schedule"]["preheat"]["started_at"]


async def test_a_manual_change_ends_the_latch(hass: HomeAssistant, hass_storage, services, freezer) -> None:
    today, room = await _preheating_room(hass, hass_storage, services, freezer)
    freezer.move_to(local(today, 5, 10))
    set_vtherm(hass, preset="eco", current=19.6, target=15.0, outdoor=5.0, user_id="ben")
    await hass.async_block_till_done()
    assert room.standdown_active and not room.preheat_latched


async def test_switching_preheat_off_ends_the_latch(hass: HomeAssistant, hass_storage, services, freezer) -> None:
    today, room = await _preheating_room(hass, hass_storage, services, freezer)
    await room.async_set_setting("preheat", False)
    assert not room.preheat_latched
    assert presets(services)[-1] == "eco"


async def test_boost_during_preheat_keeps_the_latch_and_preheat_resumes(hass: HomeAssistant, hass_storage, services, freezer) -> None:
    today, room = await _preheating_room(hass, hass_storage, services, freezer)
    freezer.move_to(local(today, 5, 0))
    boost(hass, original="comfort", end=local(today, 5, 30), current=19.3, user_id=None)
    await hass.async_block_till_done()
    assert room.standdown_active and room.preheat_latched
    freezer.move_to(local(today, 5, 30))
    restored(hass, preset="eco", target=15.0, current=20.3)  # say VTherm put back the wrong preset
    await hass.async_block_till_done()
    await hass.async_block_till_done()
    assert room.preheat_latched
    assert presets(services)[-1] == "comfort"


async def test_boost_ending_before_a_warm_by_block_hands_over(hass: HomeAssistant, hass_storage, services, freezer) -> None:
    """Acceptance 2: a boost ending 30 min before a warm-by block resumes preheat, not eco, even when the boost left the
    room warm enough that a fresh plan has nothing to preheat for (Main Bedroom, 24 Oct 06:28)."""
    today = dt_util.start_of_local_day()
    await tick(hass, freezer, local(today, 5, 0))
    seed_store(hass_storage, t_rm=10.0, settings=ALL)
    set_numbers(hass, comfort=20.5)
    set_vtherm(hass, preset="eco", current=20.8, target=15.0, outdoor=5.0)  # warm: no preheat planned
    room = await setup_room(hass, make_entry(options=opts()))
    assert not room.preheat_latched and services["set_preset"] == []
    boost(hass, original="eco", end=local(today, 6, 0), current=20.8, user_id=None)
    await hass.async_block_till_done()
    freezer.move_to(local(today, 6, 0))
    restored(hass, preset="eco", target=15.0, current=21.2)
    await hass.async_block_till_done()
    await hass.async_block_till_done()
    assert room.preheat_latched
    assert room.sched["preheat"]["why"] == "boost_ended"
    assert presets(services) == ["comfort"]
    assert hass.states.get(NEXT).state == "comfort by 06:30, preheating since 06:00"
    await tick(hass, freezer, local(today, 6, 30, 3))
    assert presets(services) == ["comfort"]


async def test_boost_ending_far_from_a_block_restores_the_current_block(hass: HomeAssistant, hass_storage, services, freezer) -> None:
    today = dt_util.start_of_local_day()
    await tick(hass, freezer, local(today, 13, 0))
    seed_store(hass_storage, t_rm=10.0, settings=ALL)
    set_numbers(hass, comfort=20.5)
    set_vtherm(hass, preset="eco", current=20.0, target=15.0, outdoor=5.0)
    room = await setup_room(hass, make_entry(options=opts()))
    boost(hass, original="eco", end=local(today, 14, 0), current=20.0, user_id=None)
    await hass.async_block_till_done()
    freezer.move_to(local(today, 14, 0))
    restored(hass, preset="eco", target=15.0, current=21.0)  # 17:00 block is three hours off
    await hass.async_block_till_done()
    await hass.async_block_till_done()
    assert not room.preheat_latched
    assert "comfort" not in presets(services)


# ---------------------------------------------------------------- settle window (D64)


async def test_a_change_just_after_hearths_own_write_is_not_learnt(hass: HomeAssistant, hass_storage, services, freezer) -> None:
    """Acceptance 3 (Hearth side): within 10 min of preheat starting a "person's" change stands Hearth down but is not
    learnt; 15 min after Hearth's last write the same change is learnt."""
    today, room = await _preheating_room(hass, hass_storage, services, freezer)  # Hearth wrote comfort at 04:47
    freezer.move_to(local(today, 4, 52))
    set_vtherm(hass, preset="boost", current=19.2, target=22.0, outdoor=5.0, user_id="ben")
    await hass.async_block_till_done()
    assert room.standdown_active
    assert room.ledger.entries == []
    assert room.learning["ignored"][-1]["reason"].startswith("settling_")


async def test_a_change_well_after_hearths_write_is_learnt(hass: HomeAssistant, hass_storage, services, freezer) -> None:
    today = dt_util.start_of_local_day()
    await tick(hass, freezer, local(today, 18, 0))
    seed_store(hass_storage, t_rm=10.0, settings={"learning": True, "adaptive": False})
    set_numbers(hass, comfort=20.5)
    set_vtherm(hass, preset="comfort", current=20.5, target=20.5)
    room = await setup_room(hass, make_entry(options={}))
    room.write_log.record("climate.living_room", "comfort", local(today, 17, 45), "schedule")  # 15 min ago
    set_vtherm(hass, preset="boost", current=20.5, target=22.0, user_id="ben")
    await hass.async_block_till_done()
    assert len(room.ledger.entries) == 1


async def test_waking_from_dormancy_opens_the_settle_window(hass: HomeAssistant, hass_storage, services, freezer) -> None:
    """Living Room, 24 Oct 13:14: eight minutes after waking from the Away pause the valve had not caught up."""
    today = dt_util.start_of_local_day()
    await tick(hass, freezer, local(today, 13, 0))
    seed_store(hass_storage, t_rm=10.0, settings={"learning": True, "adaptive": False})
    set_numbers(hass, comfort=20.5)
    set_vtherm(hass, preset="comfort", current=20.5, target=20.5, window="on")
    room = await setup_room(hass, make_entry(options={}))
    assert room.dormant
    freezer.move_to(local(today, 13, 6))
    set_vtherm(hass, preset="comfort", current=20.5, target=20.5)
    await hass.async_block_till_done()
    assert not room.dormant
    freezer.move_to(local(today, 13, 14))
    set_vtherm(hass, preset="comfort", current=20.5, target=20.0, user_id="ben")
    await hass.async_block_till_done()
    assert room.ledger.entries == []
    assert room.learning["ignored"][-1]["reason"] == "settling_dormant"
    # Past the window the same change is learnt
    freezer.move_to(local(today, 13, 30))
    set_vtherm(hass, preset="comfort", current=20.5, target=20.5, user_id="ben")
    await hass.async_block_till_done()
    room.standdown.clear()
    room.learning.pop("last_override", None)
    freezer.move_to(local(today, 16, 40))  # clear of the storm window too
    set_vtherm(hass, preset="comfort", current=20.5, target=20.0, user_id="ben")
    await hass.async_block_till_done()
    assert room.ledger.entries[-1].direction == -1


async def test_a_boost_before_warm_by_is_never_a_preheat_shortfall(hass: HomeAssistant, hass_storage, services, freezer) -> None:
    """Main Bedroom, 24 Oct 05:28: a boost during preheat, still short of target, was learnt as a +2 C preheat shortfall
    although the 07:00 warm-by had not come yet."""
    today, room = await _preheating_room(hass, hass_storage, services, freezer)
    freezer.move_to(local(today, 5, 20))  # well clear of the 04:47 write
    set_vtherm(hass, preset="comfort", current=19.6, target=20.5, outdoor=5.0, hvac_action="heating")
    await hass.async_block_till_done()
    boost(hass, original="comfort", end=local(today, 6, 20), current=19.6)
    await hass.async_block_till_done()
    assert room.standdown_active and room.preheat_latched
    assert not [e for e in room.ledger.entries if e.bucket == "preheat_shortfall"]
    assert room.learning["ignored"][-1]["reason"] == "before_warm_by"
