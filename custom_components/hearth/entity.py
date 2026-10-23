"""Base entity for Hearth."""

from __future__ import annotations

import copy
from typing import Any

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
            self._write_if_changed()

    @callback
    def _on_global_update(self) -> None:
        self._write_if_changed()

    @callback
    def _write_if_changed(self) -> None:
        """Write state only when it differs from the last write (D56).

        A room update fires on every VTherm state change, roughly every 20 s per VTherm, and used to rewrite all of the
        room's entities each time; during a startup event storm that multiplied every incoming event.
        """
        if self._fingerprint() == getattr(self, "_last_fingerprint", None):
            return
        self.async_write_ha_state()

    @callback
    def async_write_ha_state(self) -> None:
        # Every write records what it wrote, including writes from switch/number handlers and HA's own first write,
        # so a later room update never compares against a state that is no longer on screen.
        self._last_fingerprint = self._fingerprint()
        super().async_write_ha_state()

    def _fingerprint(self) -> tuple[Any, ...]:
        return (self.available, self.state, self.icon, copy.deepcopy(self.extra_state_attributes))
