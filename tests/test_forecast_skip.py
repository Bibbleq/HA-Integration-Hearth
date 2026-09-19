"""Phase 2: forecast skip, preview and setback depth through HA."""

from __future__ import annotations

from datetime import datetime, timedelta

from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.hearth.const import DOMAIN

from .conftest import ECO_NUMBER, VTHERM, WEATHER, make_entry, seed_store, set_numbers, set_vtherm, setup_room


def local(day: datetime, hh: int, mm: int = 0, ss: int = 0) -> datetime:
    return datetime.combine(day.date(), datetime.min.time(), tzinfo=dt_util.get_default_time_zone()).replace(hour=hh, minute=mm, second=ss)


def install_weather(
    hass: HomeAssistant, *, high: float = 19.0, hourly_temp: float = 12.0, morning_low: float = 5.0, condition: str = "cloudy"
):
    """Register a fake weather entity and a weather.get_forecasts responder."""
    hass.states.async_set(WEATHER, condition)
    calls = []

    async def responder(call: ServiceCall):
        calls.append(call)
        base = dt_util.start_of_local_day()
        if call.data["type"] == "hourly":
            forecast = []
            for h in range(48):
                at = base + timedelta(hours=h)
                temp = morning_low if 6 <= at.hour < 9 else hourly_temp
                forecast.append({"datetime": at.isoformat(), "temperature": temp, "condition": condition})
            return {WEATHER: {"forecast": forecast}}
        return {
            WEATHER: {
                "forecast": [
                    {
                        "datetime": (base + timedelta(days=d)).isoformat(),
                        "temperature": high,
                        "templow": morning_low,
                        "condition": condition,
                    }
                    for d in range(3)
                ]
            }
        }

    hass.services.async_register("weather", "get_forecasts", responder, supports_response=SupportsResponse.ONLY)
    return calls


async def refresh_forecast(hass: HomeAssistant) -> None:
    await hass.services.async_call(DOMAIN, "refresh_forecast", {}, blocking=True)
    await hass.async_block_till_done()


async def test_skip_decision_switches_to_eco_and_restores(hass: HomeAssistant, hass_storage, services, freezer) -> None:
    today = dt_util.start_of_local_day()
    freezer.move_to(local(today, 6, 0))
    seed_store(hass_storage, t_rm=10.0, settings={"skip": True})
    set_vtherm(hass, preset="comfort", current=18.0, outdoor=12.0)
    set_numbers(hass)
    hass.states.async_set(WEATHER, "cloudy")
    room = await setup_room(hass, make_entry(options={"weather_entity_id": WEATHER}))
    install_weather(hass, high=19.0)
    await refresh_forecast(hass)
    assert room.forecast_fresh(dt_util.utcnow())
    # Before the decision time the preview says likely
    assert hass.states.get("sensor.hearth_living_room_skip_preview").state == "likely"
    assert hass.states.get("sensor.hearth_living_room_skip_status").state == "preview"
    assert services["set_preset"] == []
    # 06:45 -> decision -> eco
    t = local(today, 6, 46)
    freezer.move_to(t)
    async_fire_time_changed(hass, t)
    await hass.async_block_till_done()
    assert len(services["set_preset"]) == 1
    assert services["set_preset"][0].data == {"entity_id": VTHERM, "preset_mode": "eco"}
    assert room.skip["status"] == "active"
    assert hass.states.get("sensor.hearth_living_room_skip_status").state == "active"
    assert hass.states.get("sensor.hearth_living_room_skip_status").attributes["previous_preset"] == "comfort"
    # VTherm reflects our change (matched to our write, so not manual)
    set_vtherm(hass, preset="eco", current=18.0, outdoor=12.0)
    await hass.async_block_till_done()
    assert room.skip["status"] == "active"
    # 16:31 -> restore comfort
    t = local(today, 16, 31)
    freezer.move_to(t)
    async_fire_time_changed(hass, t)
    await hass.async_block_till_done()
    assert services["set_preset"][-1].data == {"entity_id": VTHERM, "preset_mode": "comfort"}
    assert room.skip["status"] == "idle"
    assert room.skip["end_reason"] == "ended"
    # Nothing more happens today
    t = local(today, 17, 0)
    freezer.move_to(t)
    async_fire_time_changed(hass, t)
    await hass.async_block_till_done()
    assert len(services["set_preset"]) == 2


async def test_no_skip_when_forecast_cold_or_stale_or_disabled(hass: HomeAssistant, hass_storage, services, freezer) -> None:
    today = dt_util.start_of_local_day()
    freezer.move_to(local(today, 6, 0))
    seed_store(hass_storage, t_rm=10.0, settings={"skip": True})
    set_vtherm(hass, preset="comfort", current=18.0)
    set_numbers(hass)
    hass.states.async_set(WEATHER, "cloudy")
    room = await setup_room(hass, make_entry(options={"weather_entity_id": WEATHER}))
    # No forecast at all: stale -> preview unknown, no decision commits
    assert hass.states.get("sensor.hearth_living_room_skip_preview").state == "unknown"
    t = local(today, 6, 50)
    freezer.move_to(t)
    async_fire_time_changed(hass, t)
    await hass.async_block_till_done()
    assert services["set_preset"] == []
    assert room.skip.get("pending_reason") == "forecast_stale"
    assert room.skip.get("decided_date") is None
    # A cold forecast arrives inside the late window: decision is "forecast_cold"
    install_weather(hass, high=15.0)
    await refresh_forecast(hass)
    assert room.skip["decided_date"] == today.date().isoformat()
    assert room.skip["reason"] == "forecast_cold"
    assert services["set_preset"] == []
    assert hass.states.get("sensor.hearth_living_room_skip_status").state == "idle"


async def test_skip_missed_after_late_window(hass: HomeAssistant, hass_storage, services, freezer) -> None:
    today = dt_util.start_of_local_day()
    freezer.move_to(local(today, 10, 0))
    seed_store(hass_storage, t_rm=10.0, settings={"skip": True})
    set_vtherm(hass, preset="comfort", current=18.0)
    set_numbers(hass)
    install_weather(hass, high=25.0)
    room = await setup_room(hass, make_entry(options={"weather_entity_id": WEATHER}))
    await refresh_forecast(hass)
    assert room.skip["reason"] == "missed"
    assert services["set_preset"] == []


async def test_skip_switch_off_means_out_of_scope(hass: HomeAssistant, hass_storage, services, freezer) -> None:
    today = dt_util.start_of_local_day()
    freezer.move_to(local(today, 6, 50))
    seed_store(hass_storage, t_rm=10.0)  # skip switch default off
    set_vtherm(hass, preset="comfort", current=18.0)
    set_numbers(hass)
    install_weather(hass, high=25.0)
    room = await setup_room(hass, make_entry(options={"weather_entity_id": WEATHER}))
    await refresh_forecast(hass)
    assert room.skip["reason"] == "out_of_scope"
    assert services["set_preset"] == []
    # Preview still informs
    assert hass.states.get("sensor.hearth_living_room_skip_preview").state == "likely"
    assert hass.states.get("sensor.hearth_living_room_skip_status").state == "idle"


async def test_skip_aborts_on_indoor_floor(hass: HomeAssistant, hass_storage, services, freezer) -> None:
    today = dt_util.start_of_local_day()
    freezer.move_to(local(today, 6, 50))
    seed_store(hass_storage, t_rm=10.0, settings={"skip": True})
    set_vtherm(hass, preset="comfort", current=18.0)
    set_numbers(hass)
    install_weather(hass, high=25.0)
    room = await setup_room(hass, make_entry(options={"weather_entity_id": WEATHER}))
    await refresh_forecast(hass)
    assert room.skip["status"] == "active"
    set_vtherm(hass, preset="eco", current=15.5)
    await hass.async_block_till_done()
    assert room.skip["status"] == "aborted"
    assert room.skip["aborted_reason"] == "below_indoor_floor"
    assert services["set_preset"][-1].data["preset_mode"] == "comfort"
    assert hass.states.get("sensor.hearth_living_room_skip_status").state == "aborted"


async def test_skip_aborts_on_manual_preset_change_without_restore(hass: HomeAssistant, hass_storage, services, freezer) -> None:
    today = dt_util.start_of_local_day()
    freezer.move_to(local(today, 6, 50))
    seed_store(hass_storage, t_rm=10.0, settings={"skip": True})
    set_vtherm(hass, preset="comfort", current=18.0)
    set_numbers(hass)
    install_weather(hass, high=25.0)
    room = await setup_room(hass, make_entry(options={"weather_entity_id": WEATHER}))
    await refresh_forecast(hass)
    assert room.skip["status"] == "active"
    set_vtherm(hass, preset="eco", current=18.0)
    await hass.async_block_till_done()
    # Someone (the Scheduler, a person) puts it to boost
    freezer.tick(timedelta(minutes=10))
    set_vtherm(hass, preset="boost", current=18.0)
    await hass.async_block_till_done()
    assert room.skip["status"] == "aborted"
    assert room.skip["aborted_reason"] == "manual_intervention"
    assert len(services["set_preset"]) == 1  # no restore write


async def test_skip_aborts_on_dormant(hass: HomeAssistant, hass_storage, services, freezer) -> None:
    today = dt_util.start_of_local_day()
    freezer.move_to(local(today, 6, 50))
    seed_store(hass_storage, t_rm=10.0, settings={"skip": True})
    set_vtherm(hass, preset="comfort", current=18.0)
    set_numbers(hass)
    install_weather(hass, high=25.0)
    room = await setup_room(hass, make_entry(options={"weather_entity_id": WEATHER}))
    await refresh_forecast(hass)
    set_vtherm(hass, preset="eco", current=18.0, window="on")
    await hass.async_block_till_done()
    assert room.skip["status"] == "aborted"
    assert room.skip["aborted_reason"] == "dormant"
    assert services["set_preset"][-1].data["preset_mode"] == "comfort"


async def test_skip_mid_morning_recheck_shortfall(hass: HomeAssistant, hass_storage, services, freezer) -> None:
    today = dt_util.start_of_local_day()
    freezer.move_to(local(today, 6, 50))
    seed_store(hass_storage, t_rm=10.0, settings={"skip": True})
    set_vtherm(hass, preset="comfort", current=18.0, outdoor=12.0)
    set_numbers(hass)
    install_weather(hass, high=25.0, hourly_temp=12.0)
    room = await setup_room(hass, make_entry(options={"weather_entity_id": WEATHER}))
    await refresh_forecast(hass)
    assert room.skip["status"] == "active"
    # 11:05: indoor falling and outdoor 4 C under forecast -> abort
    t = local(today, 11, 5)
    freezer.move_to(t)
    set_vtherm(hass, preset="eco", current=17.5, outdoor=8.0)
    async_fire_time_changed(hass, t)
    await hass.async_block_till_done()
    assert room.skip["status"] == "aborted"
    assert room.skip["aborted_reason"] == "forecast_shortfall"


async def test_skip_restart_during_active_skip_is_idempotent(hass: HomeAssistant, hass_storage, services, freezer) -> None:
    today = dt_util.start_of_local_day()
    freezer.move_to(local(today, 9, 0))
    seed_store(
        hass_storage,
        t_rm=10.0,
        settings={"skip": True},
        extra={
            "mechanisms": {
                "skip": {
                    "status": "active",
                    "date": today.date().isoformat(),
                    "decided_date": today.date().isoformat(),
                    "previous_preset": "comfort",
                    "started_at": local(today, 6, 45).isoformat(),
                    "ends_at": local(today, 16, 30).isoformat(),
                    "indoor_at_start": 18.0,
                    "recheck_done": False,
                }
            }
        },
    )
    set_vtherm(hass, preset="eco", current=18.5)
    set_numbers(hass)
    install_weather(hass, high=25.0)
    room = await setup_room(hass, make_entry(options={"weather_entity_id": WEATHER}))
    await refresh_forecast(hass)
    assert room.skip["status"] == "active"
    assert services["set_preset"] == []  # no re-fire
    t = local(today, 16, 35)
    freezer.move_to(t)
    async_fire_time_changed(hass, t)
    await hass.async_block_till_done()
    assert services["set_preset"][-1].data["preset_mode"] == "comfort"


async def test_solar_room_lowers_threshold(hass: HomeAssistant, hass_storage, services, freezer) -> None:
    today = dt_util.start_of_local_day()
    freezer.move_to(local(today, 6, 50))
    seed_store(hass_storage, t_rm=10.0, settings={"skip": True})
    set_vtherm(hass, preset="comfort", current=18.0)
    set_numbers(hass)
    install_weather(hass, high=17.5, condition="sunny")
    room = await setup_room(hass, make_entry(options={"weather_entity_id": WEATHER, "solar_gain": True}))
    await refresh_forecast(hass)
    assert room.skip["status"] == "active"
    assert room.skip["threshold"] == 17.0


async def test_setback_raises_eco_and_restores(hass: HomeAssistant, hass_storage, services, freezer) -> None:
    today = dt_util.start_of_local_day()
    freezer.move_to(local(today, 21, 0))
    seed_store(hass_storage, t_rm=10.0, settings={"setback": True})
    set_vtherm(hass, preset="eco", current=18.0)
    set_numbers(hass, eco=15.0)
    install_weather(hass, high=8.0, morning_low=-1.0)
    room = await setup_room(hass, make_entry(options={"weather_entity_id": WEATHER}))
    await refresh_forecast(hass)
    assert services["set_value"] == []
    t = local(today, 21, 31)
    freezer.move_to(t)
    async_fire_time_changed(hass, t)
    await hass.async_block_till_done()
    assert services["set_value"][-1].data == {"entity_id": ECO_NUMBER, "value": 16.0}
    assert room.setback["active"] is True
    assert hass.states.get("sensor.hearth_living_room_setback_status").state == "active"
    set_numbers(hass, eco=16.0)
    # Next morning 09:01 -> restore
    t = local(today + timedelta(days=1), 9, 1)
    freezer.move_to(t)
    async_fire_time_changed(hass, t)
    await hass.async_block_till_done()
    assert services["set_value"][-1].data == {"entity_id": ECO_NUMBER, "value": 15.0}
    assert room.setback["active"] is False
    assert room.setback["restore_reason"] == "morning_recovery"


async def test_setback_leaves_manual_eco_edit_alone(hass: HomeAssistant, hass_storage, services, freezer) -> None:
    today = dt_util.start_of_local_day()
    freezer.move_to(local(today, 21, 31))
    seed_store(hass_storage, t_rm=10.0, settings={"setback": True})
    set_vtherm(hass, preset="eco", current=18.0)
    set_numbers(hass, eco=15.0)
    install_weather(hass, high=8.0, morning_low=-1.0)
    room = await setup_room(hass, make_entry(options={"weather_entity_id": WEATHER}))
    await refresh_forecast(hass)
    assert room.setback["active"] is True
    set_numbers(hass, eco=17.0)  # user changed eco overnight
    t = local(today + timedelta(days=1), 9, 1)
    freezer.move_to(t)
    async_fire_time_changed(hass, t)
    await hass.async_block_till_done()
    assert room.setback["active"] is False
    assert room.setback["restore_reason"] == "morning_recovery_left_as_is"
    assert len(services["set_value"]) == 1


async def test_setback_not_when_mild_or_stale_and_never_above_ceiling(hass: HomeAssistant, hass_storage, services, freezer) -> None:
    today = dt_util.start_of_local_day()
    freezer.move_to(local(today, 21, 31))
    seed_store(hass_storage, t_rm=10.0, settings={"setback": True})
    set_vtherm(hass, preset="eco", current=18.0)
    set_numbers(hass, eco=15.0)
    room = await setup_room(hass, make_entry(options={"weather_entity_id": WEATHER}))
    assert room.setback.get("pending_reason") == "forecast_stale"
    install_weather(hass, high=8.0, morning_low=5.0)
    await refresh_forecast(hass)
    assert room.setback["reason"] == "morning_mild"
    assert services["set_value"] == []


async def test_setback_ceiling_is_comfort_target(hass: HomeAssistant, hass_storage, services, freezer) -> None:
    today = dt_util.start_of_local_day()
    freezer.move_to(local(today, 21, 31))
    seed_store(hass_storage, t_rm=10.0, settings={"setback": True, "adaptive": False})
    set_vtherm(hass, preset="eco", current=18.0)
    set_numbers(hass, eco=20.5)  # eco already at the comfort target: no headroom
    install_weather(hass, high=8.0, morning_low=-1.0)
    room = await setup_room(hass, make_entry(options={"weather_entity_id": WEATHER}))
    await refresh_forecast(hass)
    assert room.setback["reason"] == "no_headroom"
    assert services["set_value"] == []


async def test_forecast_prefetch_and_refresh_service(hass: HomeAssistant, hass_storage, services, freezer) -> None:
    today = dt_util.start_of_local_day()
    freezer.move_to(local(today, 6, 0))
    seed_store(hass_storage, t_rm=10.0)
    set_vtherm(hass)
    set_numbers(hass)
    calls = install_weather(hass, high=19.0)
    await setup_room(hass, make_entry(options={"weather_entity_id": WEATHER}))
    assert calls == []
    t = local(today, 6, 30)  # 15 min before the 06:45 decision
    freezer.move_to(t)
    async_fire_time_changed(hass, t)
    await hass.async_block_till_done()
    assert [c.data["type"] for c in calls] == ["hourly", "daily"]
    cache = hass.data[DOMAIN]["forecast"].cache(WEATHER)
    assert cache.fetched_at is not None and len(cache.hourly) == 48 and len(cache.daily) == 3
    assert hass_storage["hearth.forecast"]["data"]["caches"][WEATHER]["source"] == WEATHER
