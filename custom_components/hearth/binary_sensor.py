"""binary_sensor.<room>_hearth_dormant."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .entity import HearthEntity
from .room import HearthRoom


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    room: HearthRoom = hass.data[DOMAIN]["rooms"][entry.entry_id]
    async_add_entities([DormantBinarySensor(room)])


class DormantBinarySensor(HearthEntity, BinarySensorEntity):
    """On when Hearth is leaving the room alone, with the reason as an attribute."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:sleep"

    def __init__(self, room: HearthRoom) -> None:
        super().__init__(room, "dormant")

    @property
    def is_on(self) -> bool:
        return self.room.dormant

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        snap = self.room.snapshot
        return {
            "reason": self.room.dormant_reason,
            "vtherm": self.room.vtherm.entity_id,
            "vtherm_preset": snap.preset,
            "vtherm_hvac_mode": snap.hvac_mode,
            "window_open": snap.window_open,
            "safety": snap.safety_on,
            "overpowering": snap.overpowering,
            "away": snap.away,
            "global_active": self.room.global_active,
        }
