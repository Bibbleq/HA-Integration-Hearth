"""Tier 1 numbers: the runtime tuning knobs per room."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberEntityDescription, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DEFAULT_COLD_MORNING_THRESHOLD,
    DEFAULT_MAX_OFFSET,
    DEFAULT_MIN_INDOOR_FLOOR,
    DEFAULT_SKIP_THRESHOLD,
    DEFAULT_SLOPE,
    DOMAIN,
    NUMBER_COLD_MORNING,
    NUMBER_MAX_OFFSET,
    NUMBER_MIN_INDOOR_FLOOR,
    NUMBER_SKIP_THRESHOLD,
    NUMBER_SLOPE,
)
from .entity import HearthEntity
from .room import HearthRoom


@dataclass(frozen=True, kw_only=True)
class HearthNumberDescription(NumberEntityDescription):
    default: float


DESCRIPTIONS: tuple[HearthNumberDescription, ...] = (
    HearthNumberDescription(
        key=NUMBER_SLOPE,
        translation_key=NUMBER_SLOPE,
        default=DEFAULT_SLOPE,
        native_min_value=0.0,
        native_max_value=0.5,
        native_step=0.01,
        icon="mdi:chart-line",
        mode=NumberMode.BOX,
    ),
    HearthNumberDescription(
        key=NUMBER_SKIP_THRESHOLD,
        translation_key=NUMBER_SKIP_THRESHOLD,
        default=DEFAULT_SKIP_THRESHOLD,
        native_min_value=5.0,
        native_max_value=30.0,
        native_step=0.5,
        device_class=NumberDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        icon="mdi:weather-sunny-alert",
        mode=NumberMode.BOX,
    ),
    HearthNumberDescription(
        key=NUMBER_MAX_OFFSET,
        translation_key=NUMBER_MAX_OFFSET,
        default=DEFAULT_MAX_OFFSET,
        native_min_value=0.0,
        native_max_value=5.0,
        native_step=0.5,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        icon="mdi:arrow-expand-vertical",
        mode=NumberMode.BOX,
    ),
    HearthNumberDescription(
        key=NUMBER_MIN_INDOOR_FLOOR,
        translation_key=NUMBER_MIN_INDOOR_FLOOR,
        default=DEFAULT_MIN_INDOOR_FLOOR,
        native_min_value=5.0,
        native_max_value=25.0,
        native_step=0.5,
        device_class=NumberDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        icon="mdi:thermometer-low",
        mode=NumberMode.BOX,
    ),
    HearthNumberDescription(
        key=NUMBER_COLD_MORNING,
        translation_key=NUMBER_COLD_MORNING,
        default=DEFAULT_COLD_MORNING_THRESHOLD,
        native_min_value=-15.0,
        native_max_value=15.0,
        native_step=0.5,
        device_class=NumberDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        icon="mdi:snowflake-thermometer",
        mode=NumberMode.BOX,
    ),
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    room: HearthRoom = hass.data[DOMAIN]["rooms"][entry.entry_id]
    async_add_entities(HearthNumber(room, description) for description in DESCRIPTIONS)


class HearthNumber(HearthEntity, NumberEntity):
    """A Tier 1 knob, persisted in the room store."""

    _attr_entity_category = EntityCategory.CONFIG
    entity_description: HearthNumberDescription

    def __init__(self, room: HearthRoom, description: HearthNumberDescription) -> None:
        super().__init__(room, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> float:
        return self.room.number(self._key)

    async def async_set_native_value(self, value: float) -> None:
        await self.room.async_set_setting(self._key, float(value))
        self.async_write_ha_state()
