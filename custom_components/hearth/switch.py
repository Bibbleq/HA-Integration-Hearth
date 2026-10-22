"""Tier 1 switches: the global kill switch and one switch per mechanism per room."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, ROOM_SWITCHES, SIGNAL_GLOBAL_UPDATE, SWITCH_DEFAULTS
from .entity import HearthEntity
from .room import HearthRoom

ICONS = {
    "adaptive": "mdi:thermometer-auto",
    "skip": "mdi:weather-sunny",
    "setback": "mdi:weather-night",
    "schedule": "mdi:calendar-clock",
    "preheat": "mdi:fire",
    "learning": "mdi:notebook-edit",
    "apply_learning": "mdi:school",
}


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    room: HearthRoom = hass.data[DOMAIN]["rooms"][entry.entry_id]
    entities: list[SwitchEntity] = [RoomMechanismSwitch(room, key) for key in ROOM_SWITCHES]
    global_state = hass.data[DOMAIN]["global"]
    if global_state.owner_entry_id == entry.entry_id:
        entities.append(GlobalActiveSwitch(hass))
        entities.append(GlobalPausedSwitch(hass))
    async_add_entities(entities)


class RoomMechanismSwitch(HearthEntity, SwitchEntity):
    """Enables one mechanism for one room."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, room: HearthRoom, key: str) -> None:
        super().__init__(room, key)
        self._attr_icon = ICONS.get(key)

    @property
    def is_on(self) -> bool:
        return self.room.switch(self._key)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"default": SWITCH_DEFAULTS[self._key], "effective": self.room.enabled(self._key)}

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.room.async_set_setting(self._key, True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.room.async_set_setting(self._key, False)
        self.async_write_ha_state()


class _GlobalSwitch(SwitchEntity):
    """Shared plumbing for the integration-wide switches."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, "global")},
            name="Hearth",
            manufacturer="Hearth",
            model="Global controls",
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(async_dispatcher_connect(self.hass, SIGNAL_GLOBAL_UPDATE, self._on_update))

    @callback
    def _on_update(self) -> None:
        self.async_write_ha_state()


class GlobalActiveSwitch(_GlobalSwitch):
    """switch.hearth_active: the global kill switch (spec section 5)."""

    _attr_translation_key = "active"
    _attr_icon = "mdi:power"
    _attr_unique_id = f"{DOMAIN}_global_active"
    _attr_extra_state_attributes = {
        "description": "Off hands every room back: Scheduler schedules Hearth had switched off are switched back on, and Hearth stops writing."
    }

    @property
    def is_on(self) -> bool:
        return bool(self.hass.data[DOMAIN]["global"].active)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.hass.data[DOMAIN]["global"].async_set_active(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.hass.data[DOMAIN]["global"].async_set_active(False)


class GlobalPausedSwitch(_GlobalSwitch):
    """switch.hearth_paused: every room dormant, nothing handed back (D45)."""

    _attr_translation_key = "paused"
    _attr_icon = "mdi:pause-circle"
    _attr_unique_id = f"{DOMAIN}_global_paused"
    _attr_extra_state_attributes = {
        "description": "On leaves every room alone but keeps ownership: Hearth-owned Scheduler schedules stay off and nothing is learnt. "
        "Off puts every room straight back on its current schedule block. switch.hearth_active off still hands rooms back."
    }

    @property
    def is_on(self) -> bool:
        return bool(self.hass.data[DOMAIN]["global"].paused)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.hass.data[DOMAIN]["global"].async_set_paused(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.hass.data[DOMAIN]["global"].async_set_paused(False)
