"""0.7.3: a VTherm timed preset (boost) stands Hearth down for exactly the boost and is learnt by when it happened (D60)."""

from __future__ import annotations

from datetime import datetime, timedelta

from homeassistant.core import Context, HomeAssistant
from homeassistant.util import dt as dt_util

from .conftest import VTHERM, make_entry, seed_store, set_numbers, setup_room, vtherm_attrs
from .conftest import set_vtherm as _set_vtherm
from .test_forecast_skip import local
from .test_schedule import opts, tick

LEARN = {"learning": True, "schedule": True}
HINT = "sensor.hearth_living_room_schedule_hint"


def boost(hass: HomeAssistant, *, original: str, end: datetime, current: float, user_id: str | None = "ben") -> None:
    attrs = vtherm_attrs(preset="boost", current=current, target=22.0)
    attrs["timed_preset_manager"] = {
        "is_active": True,
        "preset": "boost",
        "original_preset": original,
        "end_time": end.isoformat(),
        "remaining_time_min": int((end - dt_util.utcnow()).total_seconds() // 60),
    }
    hass.states.async_set(VTHERM, "heat", attrs, context=Context(user_id=user_id) if user_id else None)


def restored(hass: HomeAssistant, *, preset: str, target: float, current: float) -> None:
    attrs = vtherm_attrs(preset=preset, current=current, target=target)
    attrs["timed_preset_manager"] = {"is_active": False, "preset": None, "original_preset": None, "end_time": None}
    hass.states.async_set(VTHERM, "heat", attrs)


async def _room(hass: HomeAssistant, hass_storage, freezer, at: datetime, preset: str, target: float, current: float):
    await tick(hass, freezer, at)
    seed_store(hass_storage, t_rm=14.0, settings=LEARN, extra={"last_written": {"comfort": 20.5}})
    set_numbers(hass, comfort=20.5)
    _set_vtherm(hass, preset=preset, current=current, target=target)
    room = await setup_room(hass, make_entry(options=opts()))
    freezer.tick(timedelta(minutes=10))
    return room


async def test_boost_in_comfort_at_target_stands_down_for_the_boost_and_reads_as_comfort_too_cold(
    hass: HomeAssistant, hass_storage, services, freezer
) -> None:
    today = dt_util.start_of_local_day()
    room = await _room(hass, hass_storage, freezer, local(today, 18, 0), "comfort", 20.5, 20.5)
    end = dt_util.utcnow() + timedelta(minutes=60)
    boost(hass, original="comfort", end=end, current=20.5)
    await hass.async_block_till_done()
    assert room.standdown_active and room.standdown.cause == "timed_preset"
    assert abs((room.standdown.until - end).total_seconds()) < 2
    entry = room.ledger.entries[-1]
    assert entry.kind == "boost" and entry.direction == 1 and entry.magnitude == 0.5
    assert entry.bucket in ("baseline_error", "slope_error")


async def test_boost_while_still_warming_reads_as_a_late_warm_up(hass: HomeAssistant, hass_storage, services, freezer) -> None:
    today = dt_util.start_of_local_day()
    room = await _room(hass, hass_storage, freezer, local(today, 18, 30), "comfort", 20.5, 19.0)
    boost(hass, original="comfort", end=dt_util.utcnow() + timedelta(minutes=60), current=19.0)
    await hass.async_block_till_done()
    assert room.ledger.entries[-1].bucket == "preheat_shortfall"


async def test_the_boost_ending_is_not_an_override_and_hands_the_room_back(hass: HomeAssistant, hass_storage, services, freezer) -> None:
    today = dt_util.start_of_local_day()
    room = await _room(hass, hass_storage, freezer, local(today, 18, 0), "comfort", 20.5, 20.5)
    boost(hass, original="comfort", end=dt_util.utcnow() + timedelta(minutes=60), current=20.5)
    await hass.async_block_till_done()
    entries = len(room.ledger.entries)
    freezer.tick(timedelta(minutes=60))
    restored(hass, preset="comfort", target=20.5, current=21.5)
    await hass.async_block_till_done()
    assert not room.standdown_active
    assert room.learning["ignored"][-1]["reason"] == "boost_ended"
    assert len(room.ledger.entries) == entries


async def test_an_automations_boost_stands_down_but_is_not_learnt(hass: HomeAssistant, hass_storage, services, freezer) -> None:
    today = dt_util.start_of_local_day()
    room = await _room(hass, hass_storage, freezer, local(today, 18, 0), "comfort", 20.5, 20.5)
    boost(hass, original="comfort", end=dt_util.utcnow() + timedelta(minutes=30), current=20.5, user_id=None)
    await hass.async_block_till_done()
    assert room.standdown_active and room.standdown.cause == "timed_preset"
    assert room.ledger.entries == []
    assert room.learning["ignored"][-1]["reason"] == "automation"


async def test_repeated_boosts_in_eco_become_a_schedule_hint(hass: HomeAssistant, hass_storage, services, freezer) -> None:
    # WEEKDAY in test_schedule: eco from 09:00 until the 17:00 warm-by, so 14:10 is an eco block
    monday = dt_util.start_of_local_day(dt_util.parse_datetime("2026-10-26T12:00:00+00:00"))
    room = await _room(hass, hass_storage, freezer, local(monday, 14, 0), "eco", 15.0, 17.0)
    for day in range(3):
        at = local(monday + timedelta(days=day), 14, 10)
        freezer.move_to(at)
        boost(hass, original="eco", end=dt_util.utcnow() + timedelta(minutes=60), current=17.0)
        await hass.async_block_till_done()
        assert room.ledger.entries == []  # an eco boost says nothing about the comfort temperature
        assert room.learning["ignored"][-1]["reason"] == "schedule_pattern"
        freezer.move_to(at + timedelta(minutes=60))
        restored(hass, preset="eco", target=15.0, current=18.0)
        await hass.async_block_till_done()
    hint = hass.states.get(HINT)
    assert hint.state == "1"
    suggestion = hint.attributes["suggestions"][0]
    assert suggestion["slot"] == "14:00-15:00" and suggestion["day_type"] == "workday" and suggestion["count"] == 3


async def test_two_boosts_are_not_yet_a_pattern(hass: HomeAssistant, hass_storage, services, freezer) -> None:
    monday = dt_util.start_of_local_day(dt_util.parse_datetime("2026-10-26T12:00:00+00:00"))
    room = await _room(hass, hass_storage, freezer, local(monday, 14, 0), "eco", 15.0, 17.0)
    for day in range(2):
        freezer.move_to(local(monday + timedelta(days=day), 14, 10))
        boost(hass, original="eco", end=dt_util.utcnow() + timedelta(minutes=60), current=17.0)
        await hass.async_block_till_done()
        restored(hass, preset="eco", target=15.0, current=18.0)
        await hass.async_block_till_done()
    assert hass.states.get(HINT).state == "0"
    assert len(room.learning["boost_events"]) == 2
