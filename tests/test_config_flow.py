"""Config and options flow tests."""

from __future__ import annotations

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.hearth.const import (
    CONF_AFFECTED_PRESETS,
    CONF_BAND_MAX,
    CONF_BAND_MIN,
    CONF_BASE_TEMP,
    CONF_BEDTIME_DECISION_TIME,
    CONF_BOOST_BASE_TEMP,
    CONF_ROOM_NAME,
    CONF_SKIP_DECISION_TIME,
    CONF_SKIP_END_TIME,
    CONF_SOLAR_GAIN,
    CONF_VTHERM,
    CONF_WEATHER,
    DOMAIN,
)

from .conftest import VTHERM, WEATHER, make_entry, set_numbers, set_vtherm, setup_room

SETTINGS = {
    CONF_WEATHER: WEATHER,
    CONF_BASE_TEMP: 20.5,
    CONF_BAND_MIN: 18.0,
    CONF_BAND_MAX: 22.0,
    CONF_AFFECTED_PRESETS: ["comfort"],
    CONF_BOOST_BASE_TEMP: 22.0,
    CONF_SOLAR_GAIN: True,
    CONF_SKIP_DECISION_TIME: "06:45:00",
    CONF_SKIP_END_TIME: "16:30:00",
    CONF_BEDTIME_DECISION_TIME: "21:30:00",
}


async def test_user_flow(hass: HomeAssistant, services) -> None:
    set_vtherm(hass)
    set_numbers(hass)
    hass.states.async_set(WEATHER, "sunny")
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "user"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_VTHERM: VTHERM, CONF_ROOM_NAME: "Lounge"})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "settings"
    # Validation errors
    bad = {**SETTINGS, CONF_BAND_MIN: 23.0}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], bad)
    assert result["errors"] == {CONF_BAND_MAX: "band_inverted"}
    bad = {**SETTINGS, CONF_BASE_TEMP: 25.0}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], bad)
    assert result["errors"] == {CONF_BASE_TEMP: "base_outside_band"}
    bad = {**SETTINGS, CONF_AFFECTED_PRESETS: []}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], bad)
    assert result["errors"] == {CONF_AFFECTED_PRESETS: "no_presets"}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], SETTINGS)
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Hearth Lounge"
    assert result["data"] == {CONF_VTHERM: VTHERM, CONF_ROOM_NAME: "Lounge"}
    assert result["options"][CONF_SKIP_DECISION_TIME] == "06:45"
    assert result["options"][CONF_SOLAR_GAIN] is True
    # Duplicate VTherm aborts
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_VTHERM: VTHERM})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "already_configured"


async def test_options_flow(hass: HomeAssistant, services) -> None:
    set_vtherm(hass)
    set_numbers(hass)
    entry = make_entry()
    await setup_room(hass, entry)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "init"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {**SETTINGS, CONF_BASE_TEMP: 21.0})
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_BASE_TEMP] == 21.0
    assert entry.options[CONF_BEDTIME_DECISION_TIME] == "21:30"
    room = hass.data[DOMAIN]["rooms"][entry.entry_id]
    assert room.option(CONF_BASE_TEMP) == 21.0
