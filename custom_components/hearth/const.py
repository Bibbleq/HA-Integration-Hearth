"""Constants for the Hearth integration.

Tier 3 knobs live in the single DEFAULTS dict below. Any key can be
overridden from YAML:

    hearth:
      advanced:
        alpha: 0.75
        standdown_timeout_min: 120
"""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "hearth"
STORAGE_VERSION: Final = 1
STORAGE_KEY_ROOM: Final = "hearth.room"
STORAGE_KEY_FORECAST: Final = "hearth.forecast"
STORAGE_KEY_GLOBAL: Final = "hearth.global"

# ------------------------------------------------------------- config entry (Tier 2)
CONF_VTHERM: Final = "vtherm_entity_id"
CONF_ROOM_NAME: Final = "room_name"
CONF_WEATHER: Final = "weather_entity_id"
CONF_OUTDOOR_SENSOR: Final = "outdoor_sensor_entity_id"
CONF_BASE_TEMP: Final = "base_comfort_temp"
CONF_BAND_MIN: Final = "band_min"
CONF_BAND_MAX: Final = "band_max"
CONF_AFFECTED_PRESETS: Final = "affected_presets"
CONF_BOOST_BASE_TEMP: Final = "base_boost_temp"
CONF_SOLAR_GAIN: Final = "solar_gain"
CONF_SKIP_DECISION_TIME: Final = "skip_decision_time"
CONF_SKIP_END_TIME: Final = "skip_end_time"
CONF_BEDTIME_DECISION_TIME: Final = "bedtime_decision_time"
CONF_SCHEDULE: Final = "schedule"
CONF_WORKDAY: Final = "workday_entity_id"

PRESET_COMFORT: Final = "comfort"
PRESET_BOOST: Final = "boost"
PRESET_ECO: Final = "eco"
PRESET_FROST: Final = "frost"
PRESET_NONE: Final = "none"
PRESET_SAFETY: Final = "safety"
PRESET_POWER: Final = "power"
PRESET_ACTIVITY: Final = "activity"
AFFECTABLE_PRESETS: Final = (PRESET_COMFORT, PRESET_BOOST)

TIER2_DEFAULTS: Final = {
    CONF_BASE_TEMP: 20.5,
    CONF_BAND_MIN: 18.0,
    CONF_BAND_MAX: 22.0,
    CONF_AFFECTED_PRESETS: [PRESET_COMFORT],
    CONF_BOOST_BASE_TEMP: 22.0,
    CONF_SOLAR_GAIN: False,
    CONF_SKIP_DECISION_TIME: "06:45",
    CONF_SKIP_END_TIME: "16:30",
    CONF_BEDTIME_DECISION_TIME: "21:30",
}

# ------------------------------------------------------------- Tier 1 entity defaults
DEFAULT_SLOPE: Final = 0.15
DEFAULT_SKIP_THRESHOLD: Final = 18.0
DEFAULT_MAX_OFFSET: Final = 1.5
DEFAULT_MIN_INDOOR_FLOOR: Final = 16.0
DEFAULT_COLD_MORNING_THRESHOLD: Final = 2.0

SWITCH_ADAPTIVE: Final = "adaptive"
SWITCH_SKIP: Final = "skip"
SWITCH_SETBACK: Final = "setback"
SWITCH_SCHEDULE: Final = "schedule"
SWITCH_PREHEAT: Final = "preheat"
SWITCH_LEARNING: Final = "learning"
SWITCH_APPLY_LEARNING: Final = "apply_learning"
ROOM_SWITCHES: Final = (
    SWITCH_ADAPTIVE,
    SWITCH_SKIP,
    SWITCH_SETBACK,
    SWITCH_SCHEDULE,
    SWITCH_PREHEAT,
    SWITCH_LEARNING,
    SWITCH_APPLY_LEARNING,
)
# Phase 1 on by default, everything later off so each phase can be enabled one at a time.
SWITCH_DEFAULTS: Final = {
    SWITCH_ADAPTIVE: True,
    SWITCH_SKIP: False,
    SWITCH_SETBACK: False,
    SWITCH_SCHEDULE: False,
    SWITCH_PREHEAT: False,
    SWITCH_LEARNING: False,
    SWITCH_APPLY_LEARNING: False,
}

NUMBER_SLOPE: Final = "adaptive_slope"
NUMBER_SKIP_THRESHOLD: Final = "skip_threshold"
NUMBER_MAX_OFFSET: Final = "max_offset"
NUMBER_MIN_INDOOR_FLOOR: Final = "min_indoor_floor"
NUMBER_COLD_MORNING: Final = "cold_morning_threshold"

# ------------------------------------------------------------- Tier 3 constants
DEFAULTS: Final[dict[str, object]] = {
    # 4.1 adaptive comfort
    "alpha": 0.8,  # EN 16798 running-mean weight
    "t_ref": 10.0,  # C: running mean at which no offset applies
    "quantise_step": 0.5,  # C: Tado over HomeKit setpoint step
    "seed_hold_hours": 48,  # hours after seeding during which the offset is held at zero
    "seed_history_days": 7,  # days of recorder history used to seed T_rm
    "daily_recompute_time": "00:10",  # local HH:MM
    "outdoor_sample_interval_min": 15,  # minutes between outdoor samples for the daily mean
    "poll_interval_min": 5,  # minutes between controller evaluations
    "outdoor_stale_min": 120,  # minutes without an outdoor reading before T_rm freezes
    # 4.2 forecast skip
    "solar_boost": 1.0,  # C: threshold discount for sunny forecasts on solar-gain rooms
    "skip_threshold_from_trm": False,  # derive the base threshold from T_rm (post-warm-spell lowering)
    "skip_threshold_trm_factor": 0.25,  # C per C of T_rm above t_ref when the flag above is on
    "skip_hourly_mode": False,  # test "high before skip_hourly_cutoff" instead of the daily max
    "skip_hourly_cutoff": "14:00",
    "skip_recheck_time": "11:00",  # mid-morning re-check for the forecast-shortfall abort
    "skip_abort_outdoor_shortfall": 3.0,  # C: actual outdoor this far below forecast aborts the skip
    "skip_late_decision_window_min": 120,  # minutes after the decision time during which a missed decision is still made
    "skip_preview_time": "21:00",  # local HH:MM: when the next-day skip preview is computed
    "skip_ended_learning_window_min": 120,  # minutes after a skip ends during which bumps count as skip failures
    # 4.3 setback depth
    "setback_reduction": 1.0,  # C: eco preset raise on cold-morning nights
    "setback_restore_time": "09:00",  # local HH:MM: eco preset restored after morning recovery
    "setback_late_decision_window_min": 120,
    # 4.4 preheat
    "safety_factor": 1.15,
    "max_preheat_min": 120,
    "rate_min": 0.3,  # C/h
    "rate_max": 3.0,  # C/h
    "default_rate": 1.0,  # C/h until enough clean runs exist
    "min_learning_runs": 5,
    "max_learning_runs": 30,
    "learn_min_run_min": 20,  # minutes: shortest heating run the learner accepts
    "learn_min_rise": 0.3,  # C: smallest temperature rise the learner accepts
    "solar_gain_discount": 0.5,  # C: preheat target reduction on sunny mornings for solar rooms
    "preheat_lookahead_hours": 12,  # only plan preheat for warm-by blocks within this horizon
    # 4.6 override handling and learning
    "standdown_timeout_min": 180,
    "write_match_window_s": 180,  # seconds within which an observed change matches one of our writes
    "override_storm_window_min": 180,  # a second override within this window is ignored
    "preheat_shortfall_window_min": 60,
    "learning_half_life_days": 21,
    "learning_min_events": 3,
    "learning_target_bound": 1.0,  # C
    "learning_safety_factor_bound_pct": 25.0,
    "slope_error_cold_margin": 3.0,  # C below t_ref that counts as "cold" for slope attribution
    "write_log_len": 100,
    # 4.7 forecast subsystem
    "max_forecast_age_hours": 3,
    "forecast_refresh_interval_min": 60,
    "forecast_prefetch_lead_min": 15,
    "forecast_morning_start": "06:00",
    "forecast_morning_end": "09:00",
    "forecast_condition_window_start": "08:00",
    "forecast_condition_window_end": "14:00",
}

# Attribute paths on the VTherm climate entity (verified against VTherm 10.4).
VT_ATTR_PRESET: Final = "preset_mode"
VT_ATTR_CURRENT_TEMP: Final = "current_temperature"
VT_ATTR_TARGET_TEMP: Final = "temperature"
VT_ATTR_HVAC_ACTION: Final = "hvac_action"
VT_SPECIFIC_STATES: Final = "specific_states"
VT_ATTR_EXT_TEMP: Final = "ext_current_temperature"
VT_ATTR_SLOPE: Final = "temperature_slope"
VT_WINDOW_MANAGER: Final = "window_manager"
VT_ATTR_WINDOW_STATE: Final = "window_state"
VT_SAFETY_MANAGER: Final = "safety_manager"
VT_ATTR_SAFETY_STATE: Final = "safety_state"
VT_POWER_MANAGER: Final = "power_manager"
VT_ATTR_OVERPOWERING_STATE: Final = "overpowering_state"
VT_PRESENCE_MANAGER: Final = "presence_manager"
VT_ATTR_PRESENCE_STATE: Final = "presence_state"
VT_PRESET_NUMBER_SUFFIX: Final = "_temp"
VT_ATTR_CENTRAL_MODE: Final = "last_central_mode"
VT_CENTRAL_MODE_FROST: Final = "Frost protection"

DORMANT_AWAY: Final = "away"
DORMANT_CENTRAL_FROST: Final = "central_frost"
DORMANT_WINDOW: Final = "window"
DORMANT_SAFETY: Final = "safety"
DORMANT_OVERPOWERING: Final = "overpowering"
DORMANT_OFF: Final = "off"
DORMANT_UNAVAILABLE: Final = "unavailable"
DORMANT_DISABLED: Final = "disabled"
DORMANT_NO_PRESET_ENTITIES: Final = "no_preset_entities"

SKIP_IDLE: Final = "idle"
SKIP_PREVIEW: Final = "preview"
SKIP_ACTIVE: Final = "active"
SKIP_ABORTED: Final = "aborted"

EVENT_DIAGNOSTIC: Final = f"{DOMAIN}_diagnostic"
SIGNAL_ROOM_UPDATE: Final = f"{DOMAIN}_room_update"
SIGNAL_GLOBAL_UPDATE: Final = f"{DOMAIN}_global_update"

SERVICE_OVERRIDE: Final = "override"
SERVICE_SET_SCHEDULE: Final = "set_schedule"
SERVICE_RESET_LEARNING: Final = "reset_learning"
SERVICE_REFRESH_FORECAST: Final = "refresh_forecast"
SERVICE_RECOMPUTE: Final = "recompute"
