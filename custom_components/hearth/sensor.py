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


def all_descriptions() -> tuple[HearthSensorDescription, ...]:
    return PHASE1_SENSORS


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
