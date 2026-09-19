"""Hearth services (spec section 8)."""

from __future__ import annotations

import logging
from datetime import timedelta

import voluptuous as vol

from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv

from .const import (
    DOMAIN,
    SERVICE_OVERRIDE,
    SERVICE_RECOMPUTE,
    SERVICE_REFRESH_FORECAST,
    SERVICE_RESET_LEARNING,
    SERVICE_SET_SCHEDULE,
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

    hass.services.async_register(
        DOMAIN,
        SERVICE_OVERRIDE,
        handle_override,
        schema=vol.Schema({**ROOM_SCHEMA, vol.Optional("duration"): cv.time_period}),
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


@callback
def async_unload_services(hass: HomeAssistant) -> None:
    for service in (SERVICE_OVERRIDE, SERVICE_RECOMPUTE, SERVICE_REFRESH_FORECAST, SERVICE_SET_SCHEDULE, SERVICE_RESET_LEARNING):
        hass.services.async_remove(DOMAIN, service)
