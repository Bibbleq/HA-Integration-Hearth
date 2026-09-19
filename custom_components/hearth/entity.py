"""Base entity for Hearth."""

from __future__ import annotations

from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity

from .const import DOMAIN, SIGNAL_GLOBAL_UPDATE, SIGNAL_ROOM_UPDATE
from .room import HearthRoom


class HearthEntity(Entity):
    """An entity attached to one room's device."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, room: HearthRoom, key: str) -> None:
        self.room = room
        self._key = key
        self._attr_unique_id = f"{room.entry_id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, room.entry_id)},
            name=f"Hearth {room.room_name}",
            manufacturer="Hearth",
            model="Adaptive heating controller",
            configuration_url="https://github.com/Bibbleq/HA-Integration-Hearth",
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(async_dispatcher_connect(self.hass, SIGNAL_ROOM_UPDATE, self._on_room_update))
        self.async_on_remove(async_dispatcher_connect(self.hass, SIGNAL_GLOBAL_UPDATE, self._on_global_update))

    @callback
    def _on_room_update(self, entry_id: str) -> None:
        if entry_id == self.room.entry_id:
            self.async_write_ha_state()

    @callback
    def _on_global_update(self) -> None:
        self.async_write_ha_state()
