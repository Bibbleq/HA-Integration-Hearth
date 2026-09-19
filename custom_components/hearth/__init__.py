"""Hearth: adaptive comfort and forecast-aware heating control for Versatile Thermostat."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType

from .const import CONF_WEATHER, DEFAULTS, DOMAIN, SIGNAL_GLOBAL_UPDATE, STORAGE_KEY_GLOBAL, STORAGE_VERSION
from .forecast import ForecastManager
from .room import HearthRoom
from .services import async_setup_services, async_unload_services

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SWITCH, Platform.NUMBER, Platform.SENSOR, Platform.BINARY_SENSOR]

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                vol.Optional("advanced", default={}): vol.Schema({cv.string: vol.Any(cv.string, vol.Coerce(float), bool, int)}),
            }
        )
    },
    extra=vol.ALLOW_EXTRA,
)


class GlobalState:
    """The global kill switch, persisted independently of any room."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._store = Store(hass, STORAGE_VERSION, STORAGE_KEY_GLOBAL)
        self.active = True
        self.owner_entry_id: str | None = None

    async def async_load(self) -> None:
        data = await self._store.async_load() or {}
        self.active = bool(data.get("active", True))

    async def async_set_active(self, active: bool) -> None:
        self.active = active
        await self._store.async_save({"active": active})
        async_dispatcher_send(self.hass, SIGNAL_GLOBAL_UPDATE)
        for room in list(self.hass.data[DOMAIN]["rooms"].values()):
            await room.async_evaluate("global_switch")


def _merge_defaults(advanced: dict[str, Any]) -> dict[str, Any]:
    merged = dict(DEFAULTS)
    for key, value in (advanced or {}).items():
        if key not in DEFAULTS:
            _LOGGER.warning("hearth.advanced: unknown key %r ignored", key)
            continue
        default = DEFAULTS[key]
        try:
            if isinstance(default, bool):
                merged[key] = value if isinstance(value, bool) else cv.boolean(value)
            elif isinstance(default, int):
                merged[key] = int(value)
            elif isinstance(default, float):
                merged[key] = float(value)
            else:
                merged[key] = str(value)
        except (ValueError, TypeError, vol.Invalid):
            _LOGGER.warning("hearth.advanced: invalid value %r for %s; keeping default %r", value, key, default)
    return merged


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up shared state from YAML (Tier 3 overrides) and register services."""
    advanced = (config.get(DOMAIN) or {}).get("advanced", {})
    defaults = _merge_defaults(advanced)
    forecast = ForecastManager(hass, timedelta(minutes=float(defaults["forecast_refresh_interval_min"])))
    global_state = GlobalState(hass)
    await global_state.async_load()

    def resolve_weather(configured: str | None) -> str | None:
        """Per-room weather entity, else any other room's, else the first weather entity."""
        if configured:
            return configured
        for room in hass.data[DOMAIN]["rooms"].values():
            if candidate := room.option(CONF_WEATHER):
                return candidate
        for state in hass.states.async_all("weather"):
            return state.entity_id
        return None

    hass.data[DOMAIN] = {
        "defaults": defaults,
        "forecast": forecast,
        "global": global_state,
        "rooms": {},
        "resolve_weather": resolve_weather,
    }
    await forecast.async_load()

    async def _on_started(_event) -> None:
        await forecast.async_refresh_all()
        forecast.async_start()

    if hass.is_running:
        hass.async_create_task(_on_started(None))
    else:
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _on_started)

    async_setup_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up one room."""
    if DOMAIN not in hass.data:
        await async_setup(hass, {})
    room = HearthRoom(hass, entry)
    hass.data[DOMAIN]["rooms"][entry.entry_id] = room
    global_state: GlobalState = hass.data[DOMAIN]["global"]
    if global_state.owner_entry_id is None:
        global_state.owner_entry_id = entry.entry_id
    await room.async_setup()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload one room."""
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        room: HearthRoom | None = hass.data[DOMAIN]["rooms"].pop(entry.entry_id, None)
        if room is not None:
            await room.async_unload()
        global_state: GlobalState = hass.data[DOMAIN]["global"]
        if global_state.owner_entry_id == entry.entry_id:
            global_state.owner_entry_id = None
        if not hass.data[DOMAIN]["rooms"]:
            async_unload_services(hass)
    return ok


@callback
def get_room(hass: HomeAssistant, entry_id: str) -> HearthRoom:
    return hass.data[DOMAIN]["rooms"][entry_id]
