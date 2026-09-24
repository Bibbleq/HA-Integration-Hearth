"""Config and options flow for Hearth (Tier 2 settings)."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    AFFECTABLE_PRESETS,
    CONF_AFFECTED_PRESETS,
    CONF_BAND_MAX,
    CONF_BAND_MIN,
    CONF_BASE_TEMP,
    CONF_BEDTIME_DECISION_TIME,
    CONF_BOOST_BASE_TEMP,
    CONF_OUTDOOR_SENSOR,
    CONF_ROOM_NAME,
    CONF_SKIP_DECISION_TIME,
    CONF_SKIP_END_TIME,
    CONF_SOLAR_GAIN,
    CONF_VTHERM,
    CONF_WEATHER,
    CONF_WORKDAY,
    DOMAIN,
    TIER2_DEFAULTS,
)


def _time_default(value: str) -> str:
    return f"{value}:00" if value.count(":") == 1 else value


def _time_store(value: str) -> str:
    parts = str(value).split(":")
    return f"{int(parts[0]):02d}:{int(parts[1]):02d}"


def _tier2_schema(current: dict[str, Any]) -> vol.Schema:
    def get(key):
        return current.get(key, TIER2_DEFAULTS.get(key))

    return vol.Schema(
        {
            vol.Optional(CONF_WEATHER, description={"suggested_value": current.get(CONF_WEATHER)}): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="weather")
            ),
            vol.Optional(CONF_OUTDOOR_SENSOR, description={"suggested_value": current.get(CONF_OUTDOOR_SENSOR)}): selector.EntitySelector(
                selector.EntitySelectorConfig(domain=["sensor", "input_number"])
            ),
            vol.Optional(CONF_WORKDAY, description={"suggested_value": current.get(CONF_WORKDAY)}): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="binary_sensor", integration="workday")
            ),
            vol.Required(CONF_BASE_TEMP, default=get(CONF_BASE_TEMP)): selector.NumberSelector(
                selector.NumberSelectorConfig(min=10, max=30, step=0.5, unit_of_measurement="°C", mode=selector.NumberSelectorMode.BOX)
            ),
            vol.Required(CONF_BAND_MIN, default=get(CONF_BAND_MIN)): selector.NumberSelector(
                selector.NumberSelectorConfig(min=5, max=30, step=0.5, unit_of_measurement="°C", mode=selector.NumberSelectorMode.BOX)
            ),
            vol.Required(CONF_BAND_MAX, default=get(CONF_BAND_MAX)): selector.NumberSelector(
                selector.NumberSelectorConfig(min=5, max=30, step=0.5, unit_of_measurement="°C", mode=selector.NumberSelectorMode.BOX)
            ),
            vol.Required(CONF_AFFECTED_PRESETS, default=list(get(CONF_AFFECTED_PRESETS))): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=list(AFFECTABLE_PRESETS),
                    multiple=True,
                    translation_key="affected_presets",
                    mode=selector.SelectSelectorMode.LIST,
                )
            ),
            vol.Required(CONF_BOOST_BASE_TEMP, default=get(CONF_BOOST_BASE_TEMP)): selector.NumberSelector(
                selector.NumberSelectorConfig(min=10, max=30, step=0.5, unit_of_measurement="°C", mode=selector.NumberSelectorMode.BOX)
            ),
            vol.Required(CONF_SOLAR_GAIN, default=bool(get(CONF_SOLAR_GAIN))): selector.BooleanSelector(),
            vol.Required(CONF_SKIP_DECISION_TIME, default=_time_default(get(CONF_SKIP_DECISION_TIME))): selector.TimeSelector(),
            vol.Required(CONF_SKIP_END_TIME, default=_time_default(get(CONF_SKIP_END_TIME))): selector.TimeSelector(),
            vol.Required(CONF_BEDTIME_DECISION_TIME, default=_time_default(get(CONF_BEDTIME_DECISION_TIME))): selector.TimeSelector(),
        }
    )


def _validate(user_input: dict[str, Any]) -> dict[str, str]:
    errors: dict[str, str] = {}
    lo, hi, base = user_input.get(CONF_BAND_MIN), user_input.get(CONF_BAND_MAX), user_input.get(CONF_BASE_TEMP)
    if lo is not None and hi is not None and lo > hi:
        errors[CONF_BAND_MAX] = "band_inverted"
    elif base is not None and lo is not None and hi is not None and not (lo <= base <= hi):
        errors[CONF_BASE_TEMP] = "base_outside_band"
    if not user_input.get(CONF_AFFECTED_PRESETS):
        errors[CONF_AFFECTED_PRESETS] = "no_presets"
    return errors


def _normalise(user_input: dict[str, Any]) -> dict[str, Any]:
    out = dict(user_input)
    for key in (CONF_SKIP_DECISION_TIME, CONF_SKIP_END_TIME, CONF_BEDTIME_DECISION_TIME):
        if key in out:
            out[key] = _time_store(out[key])
    for key in (CONF_WEATHER, CONF_OUTDOOR_SENSOR, CONF_WORKDAY):
        if key in out and not out[key]:
            out.pop(key)
    return out


class HearthConfigFlow(ConfigFlow, domain=DOMAIN):
    """Two steps: pick the VTherm, then the Tier 2 settings."""

    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            await self.async_set_unique_id(user_input[CONF_VTHERM])
            self._abort_if_unique_id_configured()
            self._data = {CONF_VTHERM: user_input[CONF_VTHERM]}
            if name := user_input.get(CONF_ROOM_NAME):
                self._data[CONF_ROOM_NAME] = name
            return await self.async_step_settings()
        schema = vol.Schema(
            {
                vol.Required(CONF_VTHERM): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="climate", integration="versatile_thermostat")
                ),
                vol.Optional(CONF_ROOM_NAME): selector.TextSelector(),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_settings(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate(user_input)
            if not errors:
                options = _normalise(user_input)
                state = self.hass.states.get(self._data[CONF_VTHERM])
                title = self._data.get(CONF_ROOM_NAME) or (state.name if state else self._data[CONF_VTHERM])
                return self.async_create_entry(title=f"Hearth {title}", data=self._data, options=options)
        return self.async_show_form(step_id="settings", data_schema=_tier2_schema(user_input or {}), errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> HearthOptionsFlow:
        return HearthOptionsFlow()


class HearthOptionsFlow(OptionsFlow):
    """Edit Tier 2 settings. The schedule (phase 3) is kept as-is; it is edited via hearth.set_schedule."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate(user_input)
            if not errors:
                options = {**self.config_entry.options, **_normalise(user_input)}
                for key in (CONF_WEATHER, CONF_OUTDOOR_SENSOR, CONF_WORKDAY):
                    if key not in user_input or not user_input.get(key):
                        options.pop(key, None)
                return self.async_create_entry(title="", data=options)
        return self.async_show_form(step_id="init", data_schema=_tier2_schema(user_input or dict(self.config_entry.options)), errors=errors)
