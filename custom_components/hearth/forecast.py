"""Forecast subsystem (spec 4.7): fetch, normalise, cache, persist.

One manager per HA instance, shared by every room. Decisions never call
into here for a fetch; they read the cache a room hands them.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any

from homeassistant.components.weather import DOMAIN as WEATHER_DOMAIN
from homeassistant.components.weather import SERVICE_GET_FORECASTS
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import STORAGE_KEY_FORECAST, STORAGE_VERSION
from .core.forecast import DailyPoint, ForecastCache, HourlyPoint

_LOGGER = logging.getLogger(__name__)


def _parse_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return dt_util.as_utc(value) if value.tzinfo else value.replace(tzinfo=dt_util.UTC)
    if isinstance(value, str):
        parsed = dt_util.parse_datetime(value)
        if parsed is None:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt_util.UTC)
    return None


def normalise(hourly_raw: list[dict], daily_raw: list[dict], fetched_at: datetime, source: str) -> ForecastCache:
    """Turn `weather.get_forecasts` output into a ForecastCache."""
    hourly: list[HourlyPoint] = []
    for item in hourly_raw or []:
        at = _parse_dt(item.get("datetime"))
        temp = item.get("temperature")
        if at is None or temp is None:
            continue
        hourly.append(HourlyPoint(at, float(temp), item.get("condition")))
    daily: list[DailyPoint] = []
    for item in daily_raw or []:
        at = _parse_dt(item.get("datetime"))
        if at is None:
            continue
        day: date = dt_util.as_local(at).date()
        high = item.get("temperature")
        low = item.get("templow")
        daily.append(
            DailyPoint(day, float(high) if high is not None else None, float(low) if low is not None else None, item.get("condition"))
        )
    hourly.sort(key=lambda p: p.at)
    daily.sort(key=lambda p: p.day)
    return ForecastCache(fetched_at=fetched_at, hourly=hourly, daily=daily, source=source)


class ForecastManager:
    """Shared forecast fetcher and cache."""

    def __init__(self, hass: HomeAssistant, refresh_interval: timedelta) -> None:
        self.hass = hass
        self._store = Store(hass, STORAGE_VERSION, STORAGE_KEY_FORECAST)
        self._caches: dict[str, ForecastCache] = {}
        self._refresh_interval = refresh_interval
        self._unsub_interval = None
        self._loaded = False
        self._listeners: list = []
        self._wanted: set[str] = set()

    async def async_load(self) -> None:
        if self._loaded:
            return
        data = await self._store.async_load() or {}
        for entity_id, raw in data.get("caches", {}).items():
            try:
                self._caches[entity_id] = ForecastCache.from_dict(raw)
            except (KeyError, ValueError, TypeError) as err:
                _LOGGER.warning("Discarding corrupt forecast cache for %s: %s", entity_id, err)
        self._loaded = True

    @callback
    def async_start(self) -> None:
        if self._unsub_interval is None:
            self._unsub_interval = async_track_time_interval(
                self.hass, self._async_interval, self._refresh_interval, cancel_on_shutdown=True
            )

    @callback
    def async_stop(self) -> None:
        if self._unsub_interval is not None:
            self._unsub_interval()
            self._unsub_interval = None

    @callback
    def add_listener(self, listener) -> CALLBACK_TYPE:
        self._listeners.append(listener)

        @callback
        def _remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return _remove

    def cache(self, weather_entity_id: str | None) -> ForecastCache:
        if not weather_entity_id:
            return ForecastCache()
        return self._caches.get(weather_entity_id, ForecastCache())

    async def _async_interval(self, _now) -> None:
        await self.async_refresh_all()

    async def async_refresh_all(self) -> None:
        for entity_id in set(self._wanted):
            await self.async_refresh(entity_id)

    @callback
    def subscribe(self, weather_entity_id: str | None) -> None:
        """Register a weather entity as wanted by a room."""
        if weather_entity_id:
            self._wanted.add(weather_entity_id)

    async def async_refresh(self, weather_entity_id: str | None) -> bool:
        """Fetch hourly and daily forecasts. Returns True on success; keeps the old cache on failure."""
        if not weather_entity_id:
            return False
        if self.hass.states.get(weather_entity_id) is None:
            _LOGGER.warning("Weather entity %s not found; forecast cache left as is", weather_entity_id)
            return False
        try:
            hourly = await self._call(weather_entity_id, "hourly")
            daily = await self._call(weather_entity_id, "daily")
        except Exception as err:  # noqa: BLE001 - any failure means stale cache, never a crash
            _LOGGER.warning("Forecast fetch failed for %s: %s", weather_entity_id, err)
            return False
        if not hourly and not daily:
            _LOGGER.warning("Forecast fetch for %s returned no data", weather_entity_id)
            return False
        self._caches[weather_entity_id] = normalise(hourly, daily, dt_util.utcnow(), weather_entity_id)
        await self._async_save()
        for listener in list(self._listeners):
            listener()
        return True

    async def _call(self, weather_entity_id: str, forecast_type: str) -> list[dict]:
        response = await self.hass.services.async_call(
            WEATHER_DOMAIN,
            SERVICE_GET_FORECASTS,
            {ATTR_ENTITY_ID: weather_entity_id, "type": forecast_type},
            blocking=True,
            return_response=True,
        )
        if not response:
            return []
        payload = response.get(weather_entity_id) or {}
        return list(payload.get("forecast") or [])

    async def _async_save(self) -> None:
        await self._store.async_save({"caches": {k: v.to_dict() for k, v in self._caches.items()}})
