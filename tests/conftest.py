"""Shared fixtures for the HA-level tests."""

from __future__ import annotations

import importlib.util
from datetime import timedelta
from typing import Any

import pytest

HA_AVAILABLE = importlib.util.find_spec("homeassistant") is not None

if not HA_AVAILABLE:
    # Plain-Python run (no HA installed): only the pure core tests are collected.
    collect_ignore_glob = ["test_*.py"]
else:
    from homeassistant.core import HomeAssistant
    from homeassistant.setup import async_setup_component
    from homeassistant.util import dt as dt_util
    from pytest_homeassistant_custom_component.common import MockConfigEntry, async_mock_service

    from custom_components.hearth.const import (
        CONF_BAND_MAX,
        CONF_BAND_MIN,
        CONF_BASE_TEMP,
        CONF_ROOM_NAME,
        CONF_VTHERM,
        DOMAIN,
    )

VTHERM = "climate.living_room"
COMFORT_NUMBER = "number.living_room_preset_comfort_temp"
ECO_NUMBER = "number.living_room_preset_eco_temp"
BOOST_NUMBER = "number.living_room_preset_boost_temp"
WEATHER = "weather.home"


if HA_AVAILABLE:

    @pytest.fixture(autouse=True)
    def auto_enable_custom_integrations(enable_custom_integrations):
        """Enable loading custom_components in every test."""
        yield


@pytest.fixture(autouse=True)
def mock_recorder_seed(monkeypatch):
    if not HA_AVAILABLE:
        return
    """Recorder is not loaded in tests; seeding falls back to the current reading."""
    from custom_components.hearth import room as room_mod

    async def _no_history(hass, entity_id, days):
        return []

    monkeypatch.setattr(room_mod, "async_daily_means", _no_history)


def vtherm_attrs(
    *,
    preset: str = "comfort",
    current: float = 19.0,
    target: float = 20.5,
    outdoor: float | None = 8.0,
    hvac_action: str = "idle",
    window: str = "off",
    safety: str = "off",
    overpowering: str = "off",
    presence: str = "on",
    slope: float = 0.0,
) -> dict[str, Any]:
    return {
        "friendly_name": "Living Room",
        "preset_mode": preset,
        "preset_modes": ["none", "frost", "eco", "comfort", "boost"],
        "hvac_modes": ["off", "heat"],
        "hvac_action": hvac_action,
        "current_temperature": current,
        "temperature": target,
        "specific_states": {"ext_current_temperature": outdoor, "temperature_slope": slope, "is_on": True},
        "window_manager": {"window_state": window},
        "safety_manager": {"safety_state": safety},
        "power_manager": {"overpowering_state": overpowering},
        "presence_manager": {"presence_state": presence},
    }


def set_vtherm(hass: HomeAssistant, state: str = "heat", **kwargs) -> None:
    hass.states.async_set(VTHERM, state, vtherm_attrs(**kwargs))


def set_numbers(hass: HomeAssistant, comfort: float = 20.5, eco: float = 15.0, boost: float = 22.0) -> None:
    for entity_id, value in ((COMFORT_NUMBER, comfort), (ECO_NUMBER, eco), (BOOST_NUMBER, boost)):
        hass.states.async_set(entity_id, str(value), {"min": 5, "max": 30, "step": 0.5, "unit_of_measurement": "°C"})


@pytest.fixture
async def services(hass: HomeAssistant):
    """Mock the two VTherm write channels.

    The number and climate components are set up first so that Hearth loading
    its own number platform later does not replace the mocks with the real
    entity services.
    """
    assert await async_setup_component(hass, "number", {})
    assert await async_setup_component(hass, "climate", {})
    return {
        "set_value": async_mock_service(hass, "number", "set_value"),
        "set_preset": async_mock_service(hass, "climate", "set_preset_mode"),
    }


def make_entry(options: dict | None = None, data: dict | None = None) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="Hearth Living Room",
        unique_id=VTHERM,
        data={CONF_VTHERM: VTHERM, CONF_ROOM_NAME: "Living Room", **(data or {})},
        options={CONF_BASE_TEMP: 20.5, CONF_BAND_MIN: 18.0, CONF_BAND_MAX: 22.0, **(options or {})},
    )


def room_store_key() -> str:
    return "hearth.room.climate_living_room"


def seed_store(
    hass_storage: dict, *, t_rm: float, seeded_days_ago: float = 3, settings: dict | None = None, extra: dict | None = None
) -> None:
    """Pre-populate the room store so the 48 h seed hold is already over."""
    seeded_at = (dt_util.utcnow() - timedelta(days=seeded_days_ago)).isoformat()
    hass_storage[room_store_key()] = {
        "version": 1,
        "key": room_store_key(),
        "data": {
            "vtherm": VTHERM,
            "settings": settings or {},
            "running_mean": {
                "t_rm": t_rm,
                "t_rm_day": (dt_util.now().date() - timedelta(days=1)).isoformat(),
                "seeded_at": seeded_at,
                "frozen": False,
            },
            "write_log": [],
            "last_written": {},
            "mechanisms": {},
            **(extra or {}),
        },
    }


async def setup_room(hass: HomeAssistant, entry: MockConfigEntry):
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return hass.data[DOMAIN]["rooms"][entry.entry_id]
