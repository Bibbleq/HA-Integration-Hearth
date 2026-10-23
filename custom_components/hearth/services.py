"""Hearth services (spec section 8)."""

from __future__ import annotations

import logging
from datetime import timedelta

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv

from .const import (
    DOMAIN,
    SERVICE_BOOST,
    SERVICE_EXPORT_LEARNING,
    SERVICE_IMPORT_LEARNING,
    SERVICE_OVERRIDE,
    SERVICE_RECOMPUTE,
    SERVICE_REFRESH_FORECAST,
    SERVICE_RESET_LEARNING,
    SERVICE_SET_SCHEDULE,
    SERVICE_UNDO_RESET,
)

_LOGGER = logging.getLogger(__name__)

ROOM_SCHEMA = {
    vol.Optional("entry_id"): cv.string,
    vol.Optional("vtherm_entity_id"): cv.entity_id,
}


def _rooms_for(hass: HomeAssistant, call: ServiceCall, required: bool) -> list:
    rooms = hass.data.get(DOMAIN, {}).get("rooms", {})
    entry_id = call.data.get("entry_id")
    vtherm = call.data.get("vtherm_entity_id")
    if entry_id:
        if entry_id not in rooms:
            raise ServiceValidationError(f"No Hearth room with entry_id {entry_id}")
        return [rooms[entry_id]]
    if vtherm:
        matches = [r for r in rooms.values() if r.vtherm.entity_id == vtherm]
        if not matches:
            raise ServiceValidationError(f"No Hearth room drives {vtherm}")
        return matches
    if required:
        raise ServiceValidationError("Specify entry_id or vtherm_entity_id")
    return list(rooms.values())


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    if hass.services.has_service(DOMAIN, SERVICE_RECOMPUTE):
        return

    async def handle_override(call: ServiceCall) -> None:
        duration = call.data.get("duration")
        minutes = duration.total_seconds() / 60 if isinstance(duration, timedelta) else None
        for room in _rooms_for(hass, call, True):
            await room.async_service_override(minutes)

    async def handle_recompute(call: ServiceCall) -> None:
        for room in _rooms_for(hass, call, False):
            await room.async_evaluate("service", force_write=bool(call.data.get("force", True)))

    async def handle_refresh_forecast(call: ServiceCall) -> None:
        forecast = hass.data[DOMAIN]["forecast"]
        rooms = _rooms_for(hass, call, False)
        entities = {r.weather_entity_id for r in rooms if r.weather_entity_id}
        for entity_id in entities:
            await forecast.async_refresh(entity_id)
        for room in rooms:
            await room.async_evaluate("forecast_refresh")

    async def handle_set_schedule(call: ServiceCall) -> None:
        for room in _rooms_for(hass, call, True):
            await room.async_service_set_schedule(call.data["schedule"])

    async def handle_reset_learning(call: ServiceCall) -> None:
        for room in _rooms_for(hass, call, True):
            await room.async_service_reset_learning(call.data.get("bucket"))

    async def handle_export_learning(call: ServiceCall) -> ServiceResponse:
        # D40: a call that names one room gets that room's blob; a call that names none gets {"rooms": {...}}
        rooms = _rooms_for(hass, call, False)
        parts = call.data.get("parts")
        blobs = {room.vtherm.entity_id: await room.async_export_learning(parts) for room in rooms}
        named = bool(call.data.get("entry_id") or call.data.get("vtherm_entity_id"))
        if named and len(rooms) == 1:
            return next(iter(blobs.values()))
        return {"rooms": blobs}

    async def handle_import_learning(call: ServiceCall) -> None:
        rooms = _rooms_for(hass, call, True)
        if len(rooms) != 1:
            raise ServiceValidationError("import_learning needs exactly one room")
        await rooms[0].async_import_learning(call.data["data"], call.data.get("parts"))

    async def handle_boost(call: ServiceCall) -> None:
        from .mech_learning import BOOST_SOURCES_PERSON

        source = call.data.get("source")
        if source is None:
            source = "person" if call.context.user_id else "automation"
        rooms = _rooms_for(hass, call, True)
        for room in rooms:
            await room.async_service_boost(float(call.data["minutes"]), call.data.get("temperature"), source)
        _LOGGER.debug("boost: %s, source %s (person: %s)", [r.room_name for r in rooms], source, source in BOOST_SOURCES_PERSON)

    async def handle_undo_reset(call: ServiceCall) -> None:
        for room in _rooms_for(hass, call, True):
            await room.async_service_undo_reset()

    hass.services.async_register(
        DOMAIN,
        SERVICE_OVERRIDE,
        handle_override,
        schema=vol.Schema({**ROOM_SCHEMA, vol.Optional("duration"): cv.time_period}),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_BOOST,
        handle_boost,
        schema=vol.Schema(
            {
                **ROOM_SCHEMA,
                vol.Optional("minutes", default=60): vol.All(vol.Coerce(float), vol.Range(min=1, max=1440)),
                vol.Optional("temperature"): vol.Coerce(float),
                vol.Optional("source"): vol.In(["person", "dashboard", "valve", "voice", "automation"]),
            }
        ),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_RECOMPUTE,
        handle_recompute,
        schema=vol.Schema({**ROOM_SCHEMA, vol.Optional("force", default=True): cv.boolean}),
    )
    hass.services.async_register(DOMAIN, SERVICE_REFRESH_FORECAST, handle_refresh_forecast, schema=vol.Schema(ROOM_SCHEMA))
    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_SCHEDULE,
        handle_set_schedule,
        schema=vol.Schema({**ROOM_SCHEMA, vol.Required("schedule"): dict}),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_RESET_LEARNING,
        handle_reset_learning,
        schema=vol.Schema({**ROOM_SCHEMA, vol.Optional("bucket"): cv.string}),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_EXPORT_LEARNING,
        handle_export_learning,
        schema=vol.Schema({**ROOM_SCHEMA, vol.Optional("parts"): vol.All(cv.ensure_list, [cv.string])}),
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_IMPORT_LEARNING,
        handle_import_learning,
        schema=vol.Schema({**ROOM_SCHEMA, vol.Required("data"): dict, vol.Optional("parts"): vol.All(cv.ensure_list, [cv.string])}),
    )
    hass.services.async_register(DOMAIN, SERVICE_UNDO_RESET, handle_undo_reset, schema=vol.Schema(ROOM_SCHEMA))


@callback
def async_unload_services(hass: HomeAssistant) -> None:
    for service in (
        SERVICE_OVERRIDE,
        SERVICE_RECOMPUTE,
        SERVICE_REFRESH_FORECAST,
        SERVICE_SET_SCHEDULE,
        SERVICE_RESET_LEARNING,
        SERVICE_EXPORT_LEARNING,
        SERVICE_IMPORT_LEARNING,
        SERVICE_UNDO_RESET,
    ):
        hass.services.async_remove(DOMAIN, service)
