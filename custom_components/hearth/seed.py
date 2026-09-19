"""T_rm seeding from recorder history (spec 4.1, 9)."""

from __future__ import annotations

from datetime import date, datetime, timedelta
import logging

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

_LOGGER = logging.getLogger(__name__)


async def async_daily_means(hass: HomeAssistant, entity_id: str, days: int) -> list[float]:
    """Daily mean temperatures for `entity_id` over the last `days` days, oldest first.

    Tries long-term statistics first, then raw history. Returns [] when neither
    is available; the caller then seeds from the current reading.
    """
    end = dt_util.start_of_local_day()
    start = end - timedelta(days=days)
    means = await _from_statistics(hass, entity_id, start, end)
    if means:
        return means
    return await _from_history(hass, entity_id, start, end)


async def _from_statistics(hass: HomeAssistant, entity_id: str, start: datetime, end: datetime) -> list[float]:
    try:
        from homeassistant.components.recorder import get_instance
        from homeassistant.components.recorder.statistics import statistics_during_period

        result = await get_instance(hass).async_add_executor_job(
            statistics_during_period, hass, start, end, {entity_id}, "day", None, {"mean"}
        )
    except Exception as err:  # noqa: BLE001 - recorder may be absent or the entity may lack statistics
        _LOGGER.debug("Statistics seed unavailable for %s: %s", entity_id, err)
        return []
    rows = (result or {}).get(entity_id) or []
    means = [float(r["mean"]) for r in rows if r.get("mean") is not None]
    return means


async def _from_history(hass: HomeAssistant, entity_id: str, start: datetime, end: datetime) -> list[float]:
    try:
        from homeassistant.components.recorder import get_instance, history

        states = await get_instance(hass).async_add_executor_job(
            history.state_changes_during_period, hass, start, end, entity_id
        )
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("History seed unavailable for %s: %s", entity_id, err)
        return []
    per_day: dict[date, list[float]] = {}
    for state in (states or {}).get(entity_id, []):
        try:
            value = float(state.state)
        except (TypeError, ValueError):
            continue
        day = dt_util.as_local(state.last_updated).date()
        per_day.setdefault(day, []).append(value)
    return [sum(v) / len(v) for _, v in sorted(per_day.items())]
