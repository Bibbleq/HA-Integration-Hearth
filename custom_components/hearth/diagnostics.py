"""Diagnostics support for Hearth."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    room = hass.data[DOMAIN]["rooms"].get(entry.entry_id)
    forecast = hass.data[DOMAIN]["forecast"]
    return {
        "entry": {"data": dict(entry.data), "options": dict(entry.options)},
        "defaults": hass.data[DOMAIN]["defaults"],
        "global_active": hass.data[DOMAIN]["global"].active,
        "room": room.diagnostics() if room else None,
        "forecast": forecast.cache(room.weather_entity_id).to_dict() if room else None,
    }
