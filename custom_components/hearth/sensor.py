"""Diagnostic sensors per room (spec section 7)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorEntityDescription, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .entity import HearthEntity
from .room import HearthRoom


@dataclass(frozen=True, kw_only=True)
class HearthSensorDescription(SensorEntityDescription):
    value_fn: Callable[[HearthRoom], Any]
    attrs_fn: Callable[[HearthRoom], dict[str, Any]] | None = None


def _running_mean_attrs(room: HearthRoom) -> dict[str, Any]:
    rm = room.running_mean
    return {
        "t_rm_day": rm.t_rm_day,
        "seeded_at": rm.seeded_at,
        "seed_hold_active": room.seed_hold_active,
        "frozen": rm.frozen,
        "today_mean_so_far": round(rm.current.mean, 2) if rm.current and rm.current.mean is not None else None,
        "today_samples": rm.current.count if rm.current else 0,
        "daily_means": rm.history,
        "last_outdoor": room.last_outdoor,
        "outdoor_source": room.option("outdoor_sensor_entity_id") or room.vtherm.entity_id,
    }


def _comfort_attrs(room: HearthRoom) -> dict[str, Any]:
    c = room.comfort
    if c is None:
        return {}
    lo, hi = room.band
    return {
        "quantised": c.quantised,
        "offset_requested": round(c.offset_requested, 3),
        "offset_applied": round(c.offset_applied, 3),
        "raw_target": round(c.raw_target, 3),
        "held": c.held,
        "base_temp": room.option("base_comfort_temp"),
        "band_min": lo,
        "band_max": hi,
        "slope": room.number("adaptive_slope"),
        "max_offset": room.number("max_offset"),
        "t_ref": room.const("t_ref"),
        "affected_presets": room.affected_presets,
        "last_written": room.last_written,
        "last_write": room.last_write,
        "vtherm_comfort_temp": room.vtherm.preset_temp("comfort"),
        "adaptive_enabled": room.enabled("adaptive"),
    }


PHASE1_SENSORS: tuple[HearthSensorDescription, ...] = (
    HearthSensorDescription(
        key="running_mean_outdoor",
        translation_key="running_mean_outdoor",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_display_precision=2,
        icon="mdi:weather-partly-cloudy",
        value_fn=lambda r: r.running_mean.t_rm,
        attrs_fn=_running_mean_attrs,
    ),
    HearthSensorDescription(
        key="comfort_target",
        translation_key="comfort_target",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_display_precision=2,
        icon="mdi:home-thermometer",
        value_fn=lambda r: r.comfort.target if r.comfort else None,
        attrs_fn=_comfort_attrs,
    ),
)


def _skip_status_attrs(room: HearthRoom) -> dict[str, Any]:
    skip = room.skip
    cache = room.forecast_cache()
    return {
        "reason": skip.get("aborted_reason") or skip.get("end_reason") or skip.get("reason") or skip.get("pending_reason"),
        "decided_date": skip.get("decided_date"),
        "decided_at": skip.get("decided_at"),
        "threshold": skip.get("threshold"),
        "forecast_high": skip.get("forecast_high"),
        "condition": skip.get("condition"),
        "previous_preset": skip.get("previous_preset"),
        "started_at": skip.get("started_at"),
        "ends_at": skip.get("ends_at"),
        "ended_at": skip.get("ended_at"),
        "indoor_at_start": skip.get("indoor_at_start"),
        "skip_enabled": room.enabled("skip"),
        "effective_threshold": room.effective_skip_threshold(),
        "forecast_fresh": room.forecast_fresh(dt_util.utcnow()),
        "forecast_fetched_at": cache.fetched_at.isoformat() if cache.fetched_at else None,
        "weather_entity": room.weather_entity_id,
    }


def _skip_preview_attrs(room: HearthRoom) -> dict[str, Any]:
    return dict(room.preview)


def _setback_attrs(room: HearthRoom) -> dict[str, Any]:
    sb = room.setback
    due = room.setback_restore_due_at
    return {
        "reason": sb.get("reason") or sb.get("pending_reason"),
        "decided_date": sb.get("decided_date"),
        "decided_at": sb.get("decided_at"),
        "coldest_morning_forecast": sb.get("coldest"),
        "cold_morning_threshold": room.number("cold_morning_threshold"),
        "eco_base": sb.get("eco_base"),
        "eco_target": sb.get("eco_target"),
        "applied_at": sb.get("applied_at"),
        "restore_due_at": due.isoformat() if due else None,
        "restored_at": sb.get("restored_at"),
        "restore_reason": sb.get("restore_reason"),
        "setback_enabled": room.enabled("setback"),
        "vtherm_eco_temp": room.vtherm.preset_temp("eco"),
    }


def _preview_state(room: HearthRoom) -> str:
    likely = room.preview.get("likely")
    if likely is None:
        return "unknown"
    return "likely" if likely else "unlikely"


PHASE2_SENSORS: tuple[HearthSensorDescription, ...] = (
    HearthSensorDescription(
        key="skip_status",
        translation_key="skip_status",
        device_class=SensorDeviceClass.ENUM,
        options=["idle", "preview", "active", "aborted"],
        icon="mdi:weather-sunny",
        value_fn=lambda r: r.skip_status,
        attrs_fn=_skip_status_attrs,
    ),
    HearthSensorDescription(
        key="skip_preview",
        translation_key="skip_preview",
        device_class=SensorDeviceClass.ENUM,
        options=["likely", "unlikely", "unknown"],
        icon="mdi:crystal-ball",
        value_fn=_preview_state,
        attrs_fn=_skip_preview_attrs,
    ),
    HearthSensorDescription(
        key="setback_status",
        translation_key="setback_status",
        device_class=SensorDeviceClass.ENUM,
        options=["idle", "active"],
        icon="mdi:weather-night",
        value_fn=lambda r: r.setback_status,
        attrs_fn=_setback_attrs,
    ),
)


def all_descriptions() -> tuple[HearthSensorDescription, ...]:
    return PHASE1_SENSORS + PHASE2_SENSORS


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    room: HearthRoom = hass.data[DOMAIN]["rooms"][entry.entry_id]
    async_add_entities(HearthSensor(room, description) for description in all_descriptions())


class HearthSensor(HearthEntity, SensorEntity):
    """A read-only diagnostic."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    entity_description: HearthSensorDescription

    def __init__(self, room: HearthRoom, description: HearthSensorDescription) -> None:
        super().__init__(room, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.room)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.attrs_fn is None:
            return None
        return self.entity_description.attrs_fn(self.room)
