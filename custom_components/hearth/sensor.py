"""Diagnostic sensors per room (spec section 7)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorEntityDescription, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .entity import HearthEntity
from .room import HearthRoom


@dataclass(frozen=True, kw_only=True)
class HearthSensorDescription(SensorEntityDescription):
    value_fn: Callable[[HearthRoom], Any]
    attrs_fn: Callable[[HearthRoom], dict[str, Any]] | None = None


def _running_mean_attrs(room: HearthRoom) -> dict[str, Any]:
    rm = room.running_mean
    return {
        "t_rm_day": rm.t_rm_day,
        "seeded_at": rm.seeded_at,
        "seed_hold_active": room.seed_hold_active,
        "frozen": rm.frozen,
        "today_mean_so_far": round(rm.current.mean, 2) if rm.current and rm.current.mean is not None else None,
        "today_samples": rm.current.count if rm.current else 0,
        "daily_means": rm.history,
        "last_outdoor": room.last_outdoor,
        "outdoor_source": room.option("outdoor_sensor_entity_id") or room.vtherm.entity_id,
    }


def _comfort_attrs(room: HearthRoom) -> dict[str, Any]:
    c = room.comfort
    if c is None:
        return {}
    lo, hi = room.band
    return {
        "quantised": c.quantised,
        "offset_requested": round(c.offset_requested, 3),
        "offset_applied": round(c.offset_applied, 3),
        "raw_target": round(c.raw_target, 3),
        "held": c.held,
        "base_temp": room.option("base_comfort_temp"),
        "band_min": lo,
        "band_max": hi,
        "slope": room.number("adaptive_slope"),
        "max_offset": room.number("max_offset"),
        "t_ref": room.const("t_ref"),
        "affected_presets": room.affected_presets,
        "last_written": room.last_written,
        "last_write": room.last_write,
        "vtherm_comfort_temp": room.vtherm.preset_temp("comfort"),
        "adaptive_enabled": room.enabled("adaptive"),
    }


PHASE1_SENSORS: tuple[HearthSensorDescription, ...] = (
    HearthSensorDescription(
        key="running_mean_outdoor",
        translation_key="running_mean_outdoor",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_display_precision=2,
        icon="mdi:weather-partly-cloudy",
        value_fn=lambda r: r.running_mean.t_rm,
        attrs_fn=_running_mean_attrs,
    ),
    HearthSensorDescription(
        key="comfort_target",
        translation_key="comfort_target",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_display_precision=2,
        icon="mdi:home-thermometer",
        value_fn=lambda r: r.comfort.target if r.comfort else None,
        attrs_fn=_comfort_attrs,
    ),
)


def _skip_status_attrs(room: HearthRoom) -> dict[str, Any]:
    skip = room.skip
    cache = room.forecast_cache()
    return {
        "reason": skip.get("aborted_reason") or skip.get("end_reason") or skip.get("reason") or skip.get("pending_reason"),
        "decided_date": skip.get("decided_date"),
        "decided_at": skip.get("decided_at"),
        "threshold": skip.get("threshold"),
        "forecast_high": skip.get("forecast_high"),
        "condition": skip.get("condition"),
        "previous_preset": skip.get("previous_preset"),
        "started_at": skip.get("started_at"),
        "ends_at": skip.get("ends_at"),
        "ended_at": skip.get("ended_at"),
        "indoor_at_start": skip.get("indoor_at_start"),
        "skip_enabled": room.enabled("skip"),
        "effective_threshold": room.effective_skip_threshold(),
        "forecast_fresh": room.forecast_fresh(dt_util.utcnow()),
        "forecast_fetched_at": cache.fetched_at.isoformat() if cache.fetched_at else None,
        "weather_entity": room.weather_entity_id,
    }


def _skip_preview_attrs(room: HearthRoom) -> dict[str, Any]:
    return dict(room.preview)


def _setback_attrs(room: HearthRoom) -> dict[str, Any]:
    sb = room.setback
    due = room.setback_restore_due_at
    return {
        "reason": sb.get("reason") or sb.get("pending_reason"),
        "decided_date": sb.get("decided_date"),
        "decided_at": sb.get("decided_at"),
        "coldest_morning_forecast": sb.get("coldest"),
        "cold_morning_threshold": room.number("cold_morning_threshold"),
        "eco_base": sb.get("eco_base"),
        "eco_target": sb.get("eco_target"),
        "applied_at": sb.get("applied_at"),
        "restore_due_at": due.isoformat() if due else None,
        "restored_at": sb.get("restored_at"),
        "restore_reason": sb.get("restore_reason"),
        "setback_enabled": room.enabled("setback"),
        "vtherm_eco_temp": room.vtherm.preset_temp("eco"),
    }


def _preview_state(room: HearthRoom) -> str:
    likely = room.preview.get("likely")
    if likely is None:
        return "unknown"
    return "likely" if likely else "unlikely"


PHASE2_SENSORS: tuple[HearthSensorDescription, ...] = (
    HearthSensorDescription(
        key="skip_status",
        translation_key="skip_status",
        device_class=SensorDeviceClass.ENUM,
        options=["idle", "preview", "active", "aborted"],
        icon="mdi:weather-sunny",
        value_fn=lambda r: r.skip_status,
        attrs_fn=_skip_status_attrs,
    ),
    HearthSensorDescription(
        key="skip_preview",
        translation_key="skip_preview",
        device_class=SensorDeviceClass.ENUM,
        options=["likely", "unlikely", "unknown"],
        icon="mdi:crystal-ball",
        value_fn=_preview_state,
        attrs_fn=_skip_preview_attrs,
    ),
    HearthSensorDescription(
        key="setback_status",
        translation_key="setback_status",
        device_class=SensorDeviceClass.ENUM,
        options=["idle", "active"],
        icon="mdi:weather-night",
        value_fn=lambda r: r.setback_status,
        attrs_fn=_setback_attrs,
    ),
)


def _warming_rate_attrs(room: HearthRoom) -> dict[str, Any]:
    model = room.warming_model
    rate, learned = room.current_warming_rate()
    run = room.heating_run
    return {
        "learned": learned,
        "a": model.a,
        "b": model.b,
        "evidence_count": model.n_runs,
        "min_runs": room.const("min_learning_runs"),
        "default_rate": room.const("default_rate"),
        "rate_min": room.const("rate_min"),
        "rate_max": room.const("rate_max"),
        "outdoor": room.outdoor_temperature(),
        "vtherm_temperature_slope": room.snapshot.slope,
        "run_in_progress": run is not None,
        "run_started_at": run.started_at.isoformat() if run else None,
        "run_tainted": run.taint_reason if run and run.tainted else None,
        "recent_runs": [list(r) for r in model.runs[-5:]],
    }


def _next_block_state(room: HearthRoom) -> str | None:
    now = dt_util.utcnow()
    nxt = room.next_block(now)
    if nxt is None:
        return None
    local_start = dt_util.as_local(nxt.start)
    when = local_start.strftime("%H:%M") if local_start.date() == dt_util.now().date() else local_start.strftime("%a %H:%M")
    plan = room.preheat_plan
    if nxt.block.warm_by and room.enabled("preheat") and plan is not None and plan.block.key == nxt.key and plan.lead_min > 0:
        est = dt_util.as_local(plan.start).strftime("%H:%M")
        return f"{nxt.preset} by {when}, preheat est. {est}"
    if nxt.block.warm_by:
        return f"{nxt.preset} by {when}"
    return f"{nxt.preset} at {when}"


def _next_block_attrs(room: HearthRoom) -> dict[str, Any]:
    now = dt_util.utcnow()
    cur = room.current_block(now)
    nxt = room.next_block(now)
    plan = room.preheat_plan
    return {
        "schedule_enabled": room.enabled("schedule"),
        "preheat_enabled": room.enabled("preheat"),
        "schedule_empty": room.schedule_model.is_empty,
        "current_preset": cur.preset if cur else None,
        "current_block_start": cur.start.isoformat() if cur else None,
        "current_block_skippable": cur.block.skippable if cur else None,
        "next_preset": nxt.preset if nxt else None,
        "next_block_at": nxt.start.isoformat() if nxt else None,
        "next_block_warm_by": nxt.block.warm_by if nxt else None,
        "next_block_skippable": nxt.block.skippable if nxt else None,
        "preheat_start": plan.start.isoformat() if plan else None,
        "preheat_lead_min": round(plan.lead_min, 1) if plan else None,
        "preheat_target": plan.target if plan else None,
        "preheat_deficit": round(plan.deficit, 2) if plan else None,
        "preheat_rate": round(plan.rate, 2) if plan else None,
        "preheat_rate_learned": plan.learned if plan else None,
        "preheat_outdoor_forecast": plan.t_out if plan else None,
        "preheat_solar_discount": plan.solar_discount if plan else None,
        "applied_block": room.sched.get("applied_key"),
        "applied_by": room.sched.get("applied_by"),
        "schedule": room.schedule_model.to_dict(),
    }


PHASE3_SENSORS: tuple[HearthSensorDescription, ...] = (
    HearthSensorDescription(
        key="warming_rate",
        translation_key="warming_rate",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="°C/h",
        suggested_display_precision=2,
        icon="mdi:speedometer",
        value_fn=lambda r: r.current_warming_rate()[0],
        attrs_fn=_warming_rate_attrs,
    ),
    HearthSensorDescription(
        key="next_block",
        translation_key="next_block",
        icon="mdi:calendar-clock",
        value_fn=_next_block_state,
        attrs_fn=_next_block_attrs,
    ),
)


def _standdown_state(room: HearthRoom) -> str:
    if room.standdown_active:
        return dt_util.as_local(room.standdown.until).isoformat()
    return "none"


def _standdown_attrs(room: HearthRoom) -> dict[str, Any]:
    sd = room.standdown
    active = room.standdown_active
    return {
        "active": active,
        "cause": sd.cause if active else None,
        "started_at": sd.started_at.isoformat() if active and sd.started_at else None,
        "until": sd.until.isoformat() if active and sd.until else None,
        "last_cause": sd.cause,
        "last_external_change": room.last_external_change,
        "learning_enabled": room.enabled("learning"),
        "timeout_min": room.const("standdown_timeout_min"),
        "recent_ignored_overrides": room.learning.get("ignored", [])[-5:],
        "last_override": room.learning.get("last_override"),
    }


def _learning_sensor(bucket: str) -> HearthSensorDescription:
    return HearthSensorDescription(
        key=f"learning_{bucket}",
        translation_key=f"learning_{bucket}",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        icon="mdi:school-outline",
        value_fn=lambda r, b=bucket: round(r.ledger.buckets[b].mean, 3) if b in r.ledger.buckets else 0.0,
        attrs_fn=lambda r, b=bucket: r.learning_summary(b),
    )


PHASE4_SENSORS: tuple[HearthSensorDescription, ...] = (
    HearthSensorDescription(
        key="standdown",
        translation_key="standdown",
        icon="mdi:hand-back-left",
        value_fn=_standdown_state,
        attrs_fn=_standdown_attrs,
    ),
    _learning_sensor("preheat_shortfall"),
    _learning_sensor("slope_error"),
    _learning_sensor("skip_failure"),
    _learning_sensor("baseline_error"),
)


def all_descriptions() -> tuple[HearthSensorDescription, ...]:
    return PHASE1_SENSORS + PHASE2_SENSORS + PHASE3_SENSORS + PHASE4_SENSORS


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    room: HearthRoom = hass.data[DOMAIN]["rooms"][entry.entry_id]
    async_add_entities(HearthSensor(room, description) for description in all_descriptions())


class HearthSensor(HearthEntity, SensorEntity):
    """A read-only diagnostic."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    entity_description: HearthSensorDescription

    def __init__(self, room: HearthRoom, description: HearthSensorDescription) -> None:
        super().__init__(room, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.room)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.attrs_fn is None:
            return None
        return self.entity_description.attrs_fn(self.room)
