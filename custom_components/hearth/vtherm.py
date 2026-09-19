"""Adapter over a Versatile Thermostat climate entity.

This is the only module that touches VTherm entities. Reads come from state
attributes; writes go through exactly two channels (spec section 2): the
per-preset `number` entities and `climate.set_preset_mode`. Every temperature
write passes through `write_preset_temp`, which applies the hard clamp.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any

from homeassistant.components.climate import (
    ATTR_PRESET_MODE,
    DOMAIN as CLIMATE_DOMAIN,
    SERVICE_SET_PRESET_MODE,
    HVACAction,
    HVACMode,
)
from homeassistant.components.number import ATTR_VALUE, DOMAIN as NUMBER_DOMAIN, SERVICE_SET_VALUE
from homeassistant.const import ATTR_ENTITY_ID, STATE_NOT_HOME, STATE_OFF, STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import entity_registry as er
from homeassistant.util import slugify

from .const import (
    DORMANT_AWAY,
    DORMANT_FROST,
    DORMANT_OFF,
    DORMANT_OVERPOWERING,
    DORMANT_SAFETY,
    DORMANT_UNAVAILABLE,
    DORMANT_WINDOW,
    PRESET_FROST,
    PRESET_POWER,
    PRESET_SAFETY,
    VT_ATTR_CURRENT_TEMP,
    VT_ATTR_EXT_TEMP,
    VT_ATTR_HVAC_ACTION,
    VT_ATTR_OVERPOWERING_STATE,
    VT_ATTR_PRESENCE_STATE,
    VT_ATTR_PRESET,
    VT_ATTR_SAFETY_STATE,
    VT_ATTR_SLOPE,
    VT_ATTR_TARGET_TEMP,
    VT_ATTR_WINDOW_STATE,
    VT_POWER_MANAGER,
    VT_PRESENCE_MANAGER,
    VT_PRESET_NUMBER_SUFFIX,
    VT_SAFETY_MANAGER,
    VT_SPECIFIC_STATES,
    VT_WINDOW_MANAGER,
)

_LOGGER = logging.getLogger(__name__)


class ClampViolation(RuntimeError):
    """Raised when a write would land outside the allowed band. Never caught silently."""


def _float(value: Any) -> float | None:
    try:
        if value is None or value in (STATE_UNAVAILABLE, STATE_UNKNOWN, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _nested(attrs: dict, section: str, key: str) -> Any:
    """Read attrs[section][key], falling back to a root-level key for older VTherm layouts."""
    sec = attrs.get(section)
    if isinstance(sec, dict) and key in sec:
        return sec.get(key)
    return attrs.get(key)


@dataclass(frozen=True)
class VThermSnapshot:
    """A point-in-time read of everything Hearth needs from a VTherm."""

    available: bool
    hvac_mode: str | None
    hvac_action: str | None
    preset: str | None
    current_temp: float | None
    target_temp: float | None
    outdoor_temp: float | None
    slope: float | None
    window_open: bool
    safety_on: bool
    overpowering: bool
    away: bool

    @property
    def heating(self) -> bool:
        return self.hvac_action == HVACAction.HEATING

    @property
    def dormant_reason(self) -> str | None:
        """Why Hearth must leave this room alone, or None when it is in scope (spec 2, 5)."""
        if not self.available:
            return DORMANT_UNAVAILABLE
        if self.hvac_mode == HVACMode.OFF or self.hvac_mode is None:
            return DORMANT_OFF
        if self.safety_on or self.preset == PRESET_SAFETY:
            return DORMANT_SAFETY
        if self.overpowering or self.preset == PRESET_POWER:
            return DORMANT_OVERPOWERING
        if self.window_open:
            return DORMANT_WINDOW
        if self.away:
            return DORMANT_AWAY
        if self.preset == PRESET_FROST:
            return DORMANT_FROST
        return None


def snapshot_from_state(state: State | None) -> VThermSnapshot:
    """Build a snapshot from a climate state. Missing attributes read as 'not active'."""
    if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
        return VThermSnapshot(False, None, None, None, None, None, None, None, False, False, False, False)
    a = state.attributes
    preset = a.get(VT_ATTR_PRESET)
    window_state = _nested(a, VT_WINDOW_MANAGER, VT_ATTR_WINDOW_STATE)
    safety_state = _nested(a, VT_SAFETY_MANAGER, VT_ATTR_SAFETY_STATE)
    overpowering_state = _nested(a, VT_POWER_MANAGER, VT_ATTR_OVERPOWERING_STATE)
    presence_state = _nested(a, VT_PRESENCE_MANAGER, VT_ATTR_PRESENCE_STATE)
    return VThermSnapshot(
        available=True,
        hvac_mode=state.state,
        hvac_action=a.get(VT_ATTR_HVAC_ACTION),
        preset=str(preset) if preset is not None else None,
        current_temp=_float(a.get(VT_ATTR_CURRENT_TEMP)),
        target_temp=_float(a.get(VT_ATTR_TARGET_TEMP)),
        outdoor_temp=_float(_nested(a, VT_SPECIFIC_STATES, VT_ATTR_EXT_TEMP)),
        slope=_float(_nested(a, VT_SPECIFIC_STATES, VT_ATTR_SLOPE)),
        window_open=window_state == STATE_ON,
        safety_on=safety_state == STATE_ON,
        overpowering=overpowering_state == STATE_ON,
        away=presence_state in (STATE_OFF, STATE_NOT_HOME),
    )


class VThermAdapter:
    """Reads from and writes to one VTherm."""

    def __init__(self, hass: HomeAssistant, climate_entity_id: str) -> None:
        self.hass = hass
        self.entity_id = climate_entity_id
        self._preset_numbers: dict[str, str] = {}

    # ---------------------------------------------------------------- reads

    def snapshot(self) -> VThermSnapshot:
        return snapshot_from_state(self.hass.states.get(self.entity_id))

    @property
    def friendly_name(self) -> str:
        state = self.hass.states.get(self.entity_id)
        if state and state.name:
            return state.name
        return self.entity_id.split(".", 1)[1].replace("_", " ").title()

    def preset_number_entity(self, preset: str) -> str | None:
        """Entity id of `number.<vtherm>_preset_<preset>_temp`, resolved via the registry."""
        if preset in self._preset_numbers:
            return self._preset_numbers[preset]
        entity_id = self._resolve_preset_number(preset)
        if entity_id:
            self._preset_numbers[preset] = entity_id
        return entity_id

    def _resolve_preset_number(self, preset: str) -> str | None:
        registry = er.async_get(self.hass)
        climate_entry = registry.async_get(self.entity_id)
        suffix = f"_preset_{preset}{VT_PRESET_NUMBER_SUFFIX}"
        if climate_entry is not None and climate_entry.config_entry_id:
            for entry in er.async_entries_for_config_entry(registry, climate_entry.config_entry_id):
                if entry.domain == NUMBER_DOMAIN and entry.unique_id and str(entry.unique_id).endswith(suffix):
                    return entry.entity_id
        # Fallback: VTherm names the number after the thermostat name.
        candidates = [f"{NUMBER_DOMAIN}.{self.entity_id.split('.', 1)[1]}{suffix}"]
        if climate_entry is not None and climate_entry.original_name:
            candidates.append(f"{NUMBER_DOMAIN}.{slugify(climate_entry.original_name)}{suffix}")
        state = self.hass.states.get(self.entity_id)
        if state is not None and state.name:
            candidates.append(f"{NUMBER_DOMAIN}.{slugify(state.name)}{suffix}")
        for candidate in candidates:
            if self.hass.states.get(candidate) is not None:
                return candidate
        return None

    def preset_temp(self, preset: str) -> float | None:
        entity_id = self.preset_number_entity(preset)
        if entity_id is None:
            return None
        return _float(self.hass.states.get(entity_id).state if self.hass.states.get(entity_id) else None)

    def has_preset_numbers(self, presets: list[str]) -> bool:
        return all(self.preset_number_entity(p) is not None for p in presets)

    # ---------------------------------------------------------------- writes

    async def write_preset_temp(self, preset: str, value: float, lo: float, hi: float) -> str:
        """Write a preset temperature. Refuses anything outside [lo, hi]. Returns the entity id."""
        if lo > hi:
            raise ClampViolation(f"{self.entity_id}: inverted band [{lo}, {hi}]")
        if not (lo <= value <= hi):
            raise ClampViolation(f"{self.entity_id}: refusing to write {value} to {preset} outside [{lo}, {hi}]")
        entity_id = self.preset_number_entity(preset)
        if entity_id is None:
            raise ClampViolation(f"{self.entity_id}: no preset number entity for {preset}")
        await self.hass.services.async_call(
            NUMBER_DOMAIN,
            SERVICE_SET_VALUE,
            {ATTR_ENTITY_ID: entity_id, ATTR_VALUE: value},
            blocking=True,
        )
        return entity_id

    async def set_preset(self, preset: str) -> None:
        await self.hass.services.async_call(
            CLIMATE_DOMAIN,
            SERVICE_SET_PRESET_MODE,
            {ATTR_ENTITY_ID: self.entity_id, ATTR_PRESET_MODE: preset},
            blocking=True,
        )
