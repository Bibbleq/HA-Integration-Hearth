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

from .core.scheduler_source import (
    ISSUE_DAY_CONFLICT,
    ISSUE_SHARED_SOURCE,
    ISSUE_SOURCE_ENABLED,
    ISSUE_UNSUPPORTED_ACTION,
)

__all__ = [
    "ISSUE_DAY_CONFLICT",
    "ISSUE_SHARED_SOURCE",
    "ISSUE_SOURCE_ENABLED",
    "ISSUE_UNSUPPORTED_ACTION",
]

DOMAIN: Final = "hearth"

# Repair raised when the VTherm has no per-room preset temperature numbers and the room is not observing (D52)
ISSUE_NO_PRESET_ENTITIES: Final = "no_preset_entities"
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
CONF_SCHEDULER_ENTITIES: Final = "scheduler_entity_ids"
CONF_WARM_BY_PRESETS: Final = "warm_by_presets"
CONF_SKIPPABLE_FIRST_MORNING: Final = "skippable_first_morning"

PRESET_COMFORT: Final = "comfort"
PRESET_BOOST: Final = "boost"
PRESET_ECO: Final = "eco"
PRESET_FROST: Final = "frost"
PRESET_NONE: Final = "none"
PRESET_SAFETY: Final = "safety"
PRESET_POWER: Final = "power"
PRESET_ACTIVITY: Final = "activity"
AFFECTABLE_PRESETS: Final = (PRESET_COMFORT, PRESET_BOOST)
SCHEDULE_PRESETS: Final = (PRESET_FROST, PRESET_ECO, PRESET_COMFORT, PRESET_BOOST)

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
    CONF_WARM_BY_PRESETS: [PRESET_COMFORT, PRESET_BOOST],
    CONF_SKIPPABLE_FIRST_MORNING: True,
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
SWITCH_OBSERVE: Final = "observe"  # D49: learn, but write nothing
ROOM_SWITCHES: Final = (
    SWITCH_ADAPTIVE,
    SWITCH_SKIP,
    SWITCH_SETBACK,
    SWITCH_SCHEDULE,
    SWITCH_PREHEAT,
    SWITCH_LEARNING,
    SWITCH_APPLY_LEARNING,
    SWITCH_OBSERVE,
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
    SWITCH_OBSERVE: False,  # off for a room whose store has no such key; a brand-new room starts with it on (D49)
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
    "min_learning_runs": 5,  # runs before the delta-aware fit is trusted
    "early_rate_min_runs": 2,  # runs before the median of observed rates replaces the default (D68)
    "max_learning_runs": 30,
    "learn_min_run_min": 20,  # minutes: shortest heating run the learner accepts
    "learn_min_rise": 0.3,  # C: smallest temperature rise the learner accepts
    "learn_reach_tolerance": 0.1,  # C: a run ends (and counts) when the room is within this of target
    "learn_max_run_min": 240,  # minutes: a warm-up still short of target after this is discarded
    "solar_gain_discount": 0.5,  # C: preheat target reduction on sunny mornings for solar rooms
    "preheat_lookahead_hours": 12,  # only plan preheat for warm-by blocks within this horizon
    "boost_end_bridge_min": 45,  # a timed boost ending this close to a warm-by block hands straight over to it (D63)
    "boost_end_seen_min": 10,  # how long a boost end stays fresh for that hand-over
    # 4.6 override handling and learning
    "standdown_timeout_min": 180,
    "write_match_window_s": 180,  # seconds within which an observed change matches one of our writes
    "override_storm_window_min": 180,  # a second override within this window is ignored
    "override_coalesce_s": 15,  # seconds within which a person's stepped target changes are one override (D67)
    "preheat_shortfall_window_min": 60,
    "learning_half_life_days": 21,
    "learning_min_events": 3,
    "learning_target_bound": 1.0,  # C
    "learning_safety_factor_bound_pct": 25.0,
    "slope_error_cold_margin": 3.0,  # C below t_ref that counts as "cold" for slope attribution
    "learning_settle_min": 10,  # minutes after Hearth's own writes, a wake from dormancy, a preheat start or end or a
    # boost end during which a change is not learnt from (D64): valves and the VTherm are still catching up
    "write_log_len": 100,
    "learn_from_automations": False,  # True: changes made by automations/scripts/Scheduler are learnt from like manual ones (D47)
    # 4.7 forecast subsystem
    "max_forecast_age_hours": 3,
    "forecast_refresh_interval_min": 60,
    "forecast_prefetch_lead_min": 15,
    "forecast_morning_start": "06:00",
    "forecast_morning_end": "09:00",
    "forecast_condition_window_start": "08:00",
    "forecast_condition_window_end": "14:00",
    # 4.8 Scheduler entities as the schedule source
    "morning_cutoff": "12:00",  # local HH:MM: the first warm-by block before this is skippable
    "scheduler_enabled_check_s": 120,  # seconds after a take-over (or a re-enable) before a still-on source raises a repair
    "boost_signal_c": 0.5,  # C: ledger magnitude of one timed boost (direction only, D60)
    "boost_below_target_margin": 0.3,  # C: a boost this far below the comfort target is a late warm-up, not a cold comfort
    "boost_default_min": 60,
    "boost_min_c": 10.0,  # hearth.boost temperature clamp (D61)
    "boost_max_c": 25.0,
    "boost_pending_s": 120,  # how long a hearth.boost request waits for the VTherm timed preset to show up  # stand-down length when a timed preset reports no end time
    "boost_pattern_window_days": 28,
    "boost_pattern_min_events": 3,  # boosts in the same hour on the same kind of day before a schedule hint
    "startup_grace_s": 180,  # seconds after HA finishes starting during which a VTherm change is a restore, not an override (D55)
    "reload_grace_s": 60,  # seconds after a VTherm comes back from unavailable/not ready during which its changes are restores (D66)
    "source_reenable_grace_s": 15,  # seconds after a source turns on during which a preset change is the Scheduler's, not a person's (D48)
}

# Attribute paths on the VTherm climate entity (verified against VTherm 10.4).
VT_ATTR_PRESET: Final = "preset_mode"
VT_ATTR_CURRENT_TEMP: Final = "current_temperature"
VT_ATTR_TARGET_TEMP: Final = "temperature"
VT_ATTR_HVAC_ACTION: Final = "hvac_action"
VT_ATTR_READY: Final = "is_ready"
VT_SPECIFIC_STATES: Final = "specific_states"
VT_ATTR_EXT_TEMP: Final = "ext_current_temperature"
VT_ATTR_SLOPE: Final = "temperature_slope"
VT_WINDOW_MANAGER: Final = "window_manager"
VT_ATTR_WINDOW_STATE: Final = "window_state"
VT_ATTR_WINDOW_BYPASS: Final = "is_window_bypass"
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
DORMANT_PAUSED: Final = "paused"  # switch.hearth_paused: every room dormant, Scheduler sources stay off (D45)
# Dormancy that is the VTherm blinking (a reload, a dropped connection), not the house changing its mind: a running
# preheat and a heating run hold through it (D66)
DORMANT_TRANSIENT: Final = (DORMANT_UNAVAILABLE,)

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
# reset_learning target that clears the warming-rate model rather than a ledger bucket (D38)
RESET_WARMING_RATE: Final = "warming_rate"
SERVICE_EXPORT_LEARNING: Final = "export_learning"
SERVICE_IMPORT_LEARNING: Final = "import_learning"
SERVICE_UNDO_RESET: Final = "undo_reset"
SERVICE_REFRESH_FORECAST: Final = "refresh_forecast"
SERVICE_RECOMPUTE: Final = "recompute"
SERVICE_BOOST: Final = "boost"
