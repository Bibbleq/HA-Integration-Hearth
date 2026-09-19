"""Tests for core.forecast (spec 4.2, 4.3, 4.7)."""

from datetime import UTC, date, datetime, timedelta

import pytest

from custom_components.hearth.core.forecast import (
    DailyPoint,
    ForecastCache,
    HourlyPoint,
    decide_setback,
    decide_skip,
    derived_base_threshold,
    skip_abort_reason,
    skip_threshold,
)

UTC = UTC
T0 = datetime(2026, 1, 10, 0, 0, tzinfo=UTC)


def make_cache(fetched_at=T0, hourly_temps=None, daily=None):
    hourly = []
    for i, temp in enumerate(hourly_temps or []):
        cond = "sunny" if 9 <= i <= 13 else "cloudy"
        hourly.append(HourlyPoint(T0 + timedelta(hours=i), temp, cond))
    return ForecastCache(fetched_at=fetched_at, hourly=hourly, daily=daily or [], source="weather.test")


def test_cache_roundtrip():
    cache = make_cache(hourly_temps=[1.0, 2.0], daily=[DailyPoint(date(2026, 1, 10), 5.0, 1.0, "sunny")])
    data = cache.to_dict()
    restored = ForecastCache.from_dict(data)
    assert restored.to_dict() == data
    assert ForecastCache.from_dict(None).fetched_at is None


def test_freshness():
    cache = make_cache(fetched_at=T0)
    assert cache.is_fresh(T0 + timedelta(hours=2), timedelta(hours=3))
    assert not cache.is_fresh(T0 + timedelta(hours=4), timedelta(hours=3))
    assert not cache.is_fresh(T0 - timedelta(minutes=1), timedelta(hours=3))  # clock went backwards
    assert not ForecastCache().is_fresh(T0, timedelta(hours=3))


def test_high_for_day_daily_then_hourly():
    day = date(2026, 1, 10)
    start, end = T0, T0 + timedelta(days=1)
    cache = make_cache(hourly_temps=[5.0] * 10 + [12.0] + [7.0] * 13, daily=[DailyPoint(day, 15.0)])
    assert cache.high_for_day(day, start, end) == 15.0
    cache_no_daily = make_cache(hourly_temps=[5.0] * 10 + [12.0] + [7.0] * 13)
    assert cache_no_daily.high_for_day(day, start, end) == 12.0
    assert make_cache().high_for_day(day, start, end) is None


def test_high_for_day_hourly_cutoff_mode():
    day = date(2026, 1, 10)
    temps = [5.0] * 14 + [20.0] * 10  # warm only after 14:00
    cache = make_cache(hourly_temps=temps, daily=[DailyPoint(day, 20.0)])
    assert cache.high_for_day(day, T0, T0 + timedelta(days=1), cutoff=T0 + timedelta(hours=14)) == 5.0


def test_condition_for_day():
    day = date(2026, 1, 10)
    cache = make_cache(hourly_temps=[1.0] * 24, daily=[DailyPoint(day, 5.0, condition="rainy")])
    assert cache.condition_for_day(day, T0 + timedelta(hours=8), T0 + timedelta(hours=14)) == "rainy"
    cache = make_cache(hourly_temps=[1.0] * 24)
    assert cache.condition_for_day(day, T0 + timedelta(hours=8), T0 + timedelta(hours=14)) == "sunny"
    assert make_cache().condition_for_day(day, T0, T0 + timedelta(hours=14)) is None


def test_temperature_at_and_coldest():
    cache = make_cache(hourly_temps=[float(i) for i in range(24)])
    assert cache.temperature_at(T0 + timedelta(hours=5, minutes=20)) == 5.0
    assert cache.temperature_at(T0 + timedelta(days=3)) is None
    assert cache.coldest_between(T0 + timedelta(hours=6), T0 + timedelta(hours=9)) == 6.0
    assert cache.coldest_between(T0 + timedelta(days=2), T0 + timedelta(days=3)) is None


def test_skip_threshold_solar_discount():
    assert skip_threshold(18.0, "sunny", True, 1.0) == 17.0
    assert skip_threshold(18.0, "partlycloudy", True, 1.0) == 17.0
    assert skip_threshold(18.0, "cloudy", True, 1.0) == 18.0
    assert skip_threshold(18.0, "sunny", False, 1.0) == 18.0
    assert skip_threshold(18.0, None, True, 1.0) == 18.0


def test_derived_base_threshold_only_lowers():
    assert derived_base_threshold(18.0, None, 10.0, 0.5) == 18.0
    assert derived_base_threshold(18.0, 5.0, 10.0, 0.5) == 18.0
    assert derived_base_threshold(18.0, 14.0, 10.0, 0.5) == 16.0


def base_skip_kwargs(**over):
    kw = dict(
        forecast_fresh=True,
        forecast_high=19.0,
        condition="cloudy",
        indoor_temp=18.0,
        min_indoor_floor=16.0,
        base_threshold=18.0,
        solar_room=False,
        solar_boost=1.0,
    )
    kw.update(over)
    return kw


def test_decide_skip_happy_path():
    d = decide_skip(**base_skip_kwargs())
    assert d.skip and d.reason == "forecast_warm" and d.threshold == 18.0 and d.forecast_high == 19.0


def test_decide_skip_fails_toward_heating():
    assert decide_skip(**base_skip_kwargs(forecast_fresh=False)).reason == "forecast_stale"
    assert decide_skip(**base_skip_kwargs(forecast_high=None)).reason == "no_forecast_high"
    assert decide_skip(**base_skip_kwargs(indoor_temp=None)).reason == "no_indoor_temp"
    assert decide_skip(**base_skip_kwargs(indoor_temp=15.9)).reason == "below_indoor_floor"
    assert decide_skip(**base_skip_kwargs(in_scope=False)).reason == "out_of_scope"
    assert decide_skip(**base_skip_kwargs(forecast_high=17.9)).reason == "forecast_cold"
    for kw in (dict(forecast_fresh=False), dict(forecast_high=None), dict(indoor_temp=15.0), dict(in_scope=False)):
        assert decide_skip(**base_skip_kwargs(**kw)).skip is False


def test_decide_skip_solar_room_lowers_threshold():
    d = decide_skip(**base_skip_kwargs(forecast_high=17.2, condition="sunny", solar_room=True))
    assert d.skip and d.threshold == 17.0
    d = decide_skip(**base_skip_kwargs(forecast_high=17.2, condition="cloudy", solar_room=True))
    assert not d.skip


def base_abort_kwargs(**over):
    kw = dict(
        indoor_temp=18.0,
        min_indoor_floor=16.0,
        recheck_due=False,
        indoor_at_start=18.0,
        outdoor_actual=10.0,
        outdoor_forecast=10.0,
        outdoor_shortfall=3.0,
        manual_intervention=False,
        dormant=False,
    )
    kw.update(over)
    return kw


def test_skip_abort_reasons():
    assert skip_abort_reason(**base_abort_kwargs()) is None
    assert skip_abort_reason(**base_abort_kwargs(indoor_temp=15.5)) == "below_indoor_floor"
    assert skip_abort_reason(**base_abort_kwargs(manual_intervention=True)) == "manual_intervention"
    assert skip_abort_reason(**base_abort_kwargs(dormant=True)) == "dormant"
    # Floor beats everything
    assert skip_abort_reason(**base_abort_kwargs(indoor_temp=15.0, dormant=True)) == "below_indoor_floor"


def test_skip_abort_forecast_shortfall_needs_all_three():
    # falling indoor + outdoor 3C below forecast at recheck
    assert (
        skip_abort_reason(**base_abort_kwargs(recheck_due=True, indoor_temp=17.5, outdoor_actual=6.9, outdoor_forecast=10.0))
        == "forecast_shortfall"
    )
    # not falling
    assert skip_abort_reason(**base_abort_kwargs(recheck_due=True, indoor_temp=18.0, outdoor_actual=5.0)) is None
    # falling but outdoor on forecast
    assert skip_abort_reason(**base_abort_kwargs(recheck_due=True, indoor_temp=17.5, outdoor_actual=9.0)) is None
    # recheck not due
    assert skip_abort_reason(**base_abort_kwargs(recheck_due=False, indoor_temp=17.5, outdoor_actual=5.0)) is None
    # missing forecast -> no abort on this rule
    assert skip_abort_reason(**base_abort_kwargs(recheck_due=True, indoor_temp=17.5, outdoor_actual=5.0, outdoor_forecast=None)) is None


def test_decide_setback():
    d = decide_setback(
        forecast_fresh=True, coldest_morning=-1.0, cold_morning_threshold=2.0, eco_base=15.0, setback_reduction=1.0, ceiling=22.0
    )
    assert d.reduce and d.eco_target == 16.0 and d.reason == "cold_morning"
    d = decide_setback(
        forecast_fresh=True, coldest_morning=3.0, cold_morning_threshold=2.0, eco_base=15.0, setback_reduction=1.0, ceiling=22.0
    )
    assert not d.reduce and d.reason == "morning_mild"
    assert (
        decide_setback(
            forecast_fresh=False, coldest_morning=-1.0, cold_morning_threshold=2.0, eco_base=15.0, setback_reduction=1.0, ceiling=22.0
        ).reason
        == "forecast_stale"
    )
    assert (
        decide_setback(
            forecast_fresh=True, coldest_morning=None, cold_morning_threshold=2.0, eco_base=15.0, setback_reduction=1.0, ceiling=22.0
        ).reason
        == "no_morning_forecast"
    )
    assert (
        decide_setback(
            forecast_fresh=True, coldest_morning=-1.0, cold_morning_threshold=2.0, eco_base=None, setback_reduction=1.0, ceiling=22.0
        ).reason
        == "no_eco_base"
    )


def test_decide_setback_ceiling_is_hard():
    d = decide_setback(
        forecast_fresh=True, coldest_morning=-1.0, cold_morning_threshold=2.0, eco_base=21.5, setback_reduction=1.0, ceiling=22.0
    )
    assert d.reduce and d.eco_target == 22.0
    d = decide_setback(
        forecast_fresh=True, coldest_morning=-1.0, cold_morning_threshold=2.0, eco_base=22.0, setback_reduction=1.0, ceiling=22.0
    )
    assert not d.reduce and d.reason == "no_headroom"


@pytest.mark.parametrize("eco_base", [10.0, 15.0, 21.9, 22.0, 25.0])
def test_decide_setback_never_above_ceiling(eco_base):
    d = decide_setback(
        forecast_fresh=True, coldest_morning=-5.0, cold_morning_threshold=2.0, eco_base=eco_base, setback_reduction=1.0, ceiling=22.0
    )
    if d.reduce:
        assert d.eco_target <= 22.0
