"""Per-room controller: the HA glue that drives the core maths.

One `HearthRoom` per config entry. Everything it does is recompute-from-state
(spec 10): `async_evaluate` can run any number of times and only writes to
VTherm when the desired value has changed since the last write Hearth made.
"""

from __future__ import annotations

import logging
from datetime import datetime, time, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import (
    EventStateChangedData,
    async_track_state_change_event,
    async_track_time_change,
    async_track_time_interval,
)
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
from homeassistant.util import slugify

from .const import (
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
    CONF_VTHERM,
    CONF_WEATHER,
    DEFAULT_COLD_MORNING_THRESHOLD,
    DEFAULT_MAX_OFFSET,
    DEFAULT_MIN_INDOOR_FLOOR,
    DEFAULT_SKIP_THRESHOLD,
    DEFAULT_SLOPE,
    DOMAIN,
    DORMANT_DISABLED,
    DORMANT_NO_PRESET_ENTITIES,
    EVENT_DIAGNOSTIC,
    NUMBER_COLD_MORNING,
    NUMBER_MAX_OFFSET,
    NUMBER_MIN_INDOOR_FLOOR,
    NUMBER_SKIP_THRESHOLD,
    NUMBER_SLOPE,
    PRESET_BOOST,
    PRESET_COMFORT,
    SIGNAL_ROOM_UPDATE,
    STORAGE_KEY_ROOM,
    STORAGE_VERSION,
    SWITCH_ADAPTIVE,
    SWITCH_DEFAULTS,
    TIER2_DEFAULTS,
)
from .core.comfort import ComfortResult, RunningMeanState, RunningMeanTracker, comfort_target, seed_running_mean
from .core.override import WriteLog
from .mech_setback import SetbackMixin
from .mech_skip import SkipMixin
from .seed import async_daily_means
from .vtherm import ClampViolation, VThermAdapter, VThermSnapshot

_LOGGER = logging.getLogger(__name__)

NUMBER_DEFAULTS = {
    NUMBER_SLOPE: DEFAULT_SLOPE,
    NUMBER_SKIP_THRESHOLD: DEFAULT_SKIP_THRESHOLD,
    NUMBER_MAX_OFFSET: DEFAULT_MAX_OFFSET,
    NUMBER_MIN_INDOOR_FLOOR: DEFAULT_MIN_INDOOR_FLOOR,
    NUMBER_COLD_MORNING: DEFAULT_COLD_MORNING_THRESHOLD,
}


def parse_hhmm(value: str, fallback: str) -> time:
    for candidate in (value, fallback):
        try:
            hh, mm = str(candidate).split(":")
            return time(int(hh), int(mm))
        except (ValueError, AttributeError):
            continue
    return time(0, 0)


class HearthRoom(SkipMixin, SetbackMixin):
    """Controller for one VTherm."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.vtherm = VThermAdapter(hass, entry.data[CONF_VTHERM])
        self._store = Store(hass, STORAGE_VERSION, f"{STORAGE_KEY_ROOM}.{slugify(entry.data[CONF_VTHERM])}")
        self._unsubs: list[CALLBACK_TYPE] = []
        self._evaluating = False
        self._pending_evaluate = False

        # Persistent state
        self.settings: dict[str, Any] = {}
        self.running_mean = RunningMeanState()
        self.tracker = RunningMeanTracker(self.running_mean)
        self.write_log = WriteLog()
        self.last_written: dict[str, float] = {}
        self.last_outdoor_sample_at: datetime | None = None
        self.last_outdoor_seen_at: datetime | None = None
        self.last_outdoor: float | None = None
        self.mechanisms: dict[str, Any] = {}  # per-mechanism state blobs (skip, setback, schedule, learning)

        # Derived, in-memory
        self.snapshot: VThermSnapshot = self.vtherm.snapshot()
        self.comfort: ComfortResult | None = None
        self.dormant_reason: str | None = None
        self.last_evaluated: datetime | None = None
        self.last_write: dict[str, Any] | None = None
        self.preset_entities_ok = True
        self.last_external_change_at: datetime | None = None
        self.last_external_change: dict[str, Any] | None = None
        self._outdoor_frozen_reported = False
        self._missing_presets_reported = False

    # ------------------------------------------------------------- config access

    @property
    def entry_id(self) -> str:
        return self.entry.entry_id

    @property
    def room_name(self) -> str:
        return self.entry.data.get(CONF_ROOM_NAME) or self.vtherm.friendly_name

    def option(self, key: str, default: Any = None) -> Any:
        if key in self.entry.options:
            return self.entry.options[key]
        if key in self.entry.data:
            return self.entry.data[key]
        return TIER2_DEFAULTS.get(key, default)

    def const(self, key: str) -> Any:
        """Tier 3 constant, honouring `hearth: advanced:` overrides."""
        return self.hass.data[DOMAIN]["defaults"][key]

    @property
    def weather_entity_id(self) -> str | None:
        return self.hass.data[DOMAIN]["resolve_weather"](self.option(CONF_WEATHER))

    @property
    def affected_presets(self) -> list[str]:
        presets = self.option(CONF_AFFECTED_PRESETS) or [PRESET_COMFORT]
        return [p for p in presets if p in (PRESET_COMFORT, PRESET_BOOST)]

    @property
    def band(self) -> tuple[float, float]:
        lo = float(self.option(CONF_BAND_MIN))
        hi = float(self.option(CONF_BAND_MAX))
        return (lo, hi) if lo <= hi else (hi, hi)

    def switch(self, key: str) -> bool:
        return bool(self.settings.get(key, SWITCH_DEFAULTS[key]))

    def number(self, key: str) -> float:
        return float(self.settings.get(key, NUMBER_DEFAULTS[key]))

    @property
    def global_active(self) -> bool:
        return bool(self.hass.data[DOMAIN]["global"].active)

    def enabled(self, key: str) -> bool:
        return self.global_active and self.switch(key)

    async def async_set_setting(self, key: str, value: Any) -> None:
        self.settings[key] = value
        self._schedule_save()
        await self.async_evaluate("setting")

    # ------------------------------------------------------------- lifecycle

    async def async_setup(self) -> None:
        await self._async_load()
        self.hass.data[DOMAIN]["forecast"].subscribe(self.weather_entity_id)
        await self._async_seed_if_needed()
        self._unsubs.append(async_track_state_change_event(self.hass, [self.vtherm.entity_id], self._on_vtherm_event))
        if outdoor := self.option(CONF_OUTDOOR_SENSOR):
            self._unsubs.append(async_track_state_change_event(self.hass, [outdoor], self._on_outdoor_event))
        poll = timedelta(minutes=float(self.const("poll_interval_min")))
        self._unsubs.append(async_track_time_interval(self.hass, self._on_tick, poll, cancel_on_shutdown=True))
        roll = parse_hhmm(self.const("daily_recompute_time"), "00:10")
        self._unsubs.append(self._track_time(self._on_tick, roll.hour, roll.minute, 0))
        self._register_time_triggers()
        await self.async_evaluate("setup")

    def _register_time_triggers(self) -> None:
        """Exact-time triggers for decision times, plus forecast prefetch 15 min before each."""
        lead = timedelta(minutes=float(self.const("forecast_prefetch_lead_min")))
        decision_times = [
            parse_hhmm(self.option(CONF_SKIP_DECISION_TIME), TIER2_DEFAULTS[CONF_SKIP_DECISION_TIME]),
            parse_hhmm(self.option(CONF_BEDTIME_DECISION_TIME), TIER2_DEFAULTS[CONF_BEDTIME_DECISION_TIME]),
        ]
        other_times = [
            parse_hhmm(self.option(CONF_SKIP_END_TIME), TIER2_DEFAULTS[CONF_SKIP_END_TIME]),
            parse_hhmm(self.const("skip_recheck_time"), "11:00"),
            parse_hhmm(self.const("setback_restore_time"), "09:00"),
            parse_hhmm(self.const("skip_preview_time"), "21:00"),
        ]
        for t in decision_times + other_times:
            self._unsubs.append(async_track_time_change(self.hass, self._on_tick, hour=t.hour, minute=t.minute, second=5))
        for t in decision_times:
            prefetch = (datetime.combine(datetime(2000, 1, 1), t) - lead).time()
            self._unsubs.append(async_track_time_change(self.hass, self._on_prefetch, hour=prefetch.hour, minute=prefetch.minute, second=0))
        self.hass.data[DOMAIN]["forecast"].add_listener(self._on_forecast_update)

    def _track_time(self, action, hour: int, minute: int, second: int) -> CALLBACK_TYPE:
        return async_track_time_change(self.hass, action, hour=hour, minute=minute, second=second)

    @callback
    def _on_prefetch(self, _now) -> None:
        self.hass.async_create_task(self._async_prefetch())

    async def _async_prefetch(self) -> None:
        await self.hass.data[DOMAIN]["forecast"].async_refresh(self.weather_entity_id)
        await self.async_evaluate("prefetch")

    @callback
    def _on_forecast_update(self) -> None:
        self.hass.async_create_task(self.async_evaluate("forecast"))

    async def async_unload(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        await self._store.async_save(self._data_to_save())

    # ------------------------------------------------------------- persistence

    async def _async_load(self) -> None:
        data = await self._store.async_load() or {}
        self.settings = dict(data.get("settings", {}))
        self.running_mean = RunningMeanState.from_dict(data.get("running_mean"))
        self.tracker = RunningMeanTracker(self.running_mean, alpha=float(self.const("alpha")))
        self.write_log = WriteLog.from_list(data.get("write_log"), int(self.const("write_log_len")))
        self.last_written = {k: float(v) for k, v in data.get("last_written", {}).items()}
        self.last_outdoor_sample_at = _dt(data.get("last_outdoor_sample_at"))
        self.last_outdoor_seen_at = _dt(data.get("last_outdoor_seen_at"))
        self.last_outdoor = data.get("last_outdoor")
        self.mechanisms = dict(data.get("mechanisms", {}))
        self.last_external_change_at = _dt(data.get("last_external_change_at"))
        self.last_external_change = data.get("last_external_change")
        self._load_mechanisms()

    def _load_mechanisms(self) -> None:
        """Later phases hydrate their state objects from self.mechanisms here."""

    def _dump_mechanisms(self) -> dict:
        return dict(self.mechanisms)

    def _data_to_save(self) -> dict:
        return {
            "vtherm": self.vtherm.entity_id,
            "settings": self.settings,
            "running_mean": self.running_mean.to_dict(),
            "write_log": self.write_log.to_list(),
            "last_written": self.last_written,
            "last_outdoor_sample_at": _iso(self.last_outdoor_sample_at),
            "last_outdoor_seen_at": _iso(self.last_outdoor_seen_at),
            "last_outdoor": self.last_outdoor,
            "mechanisms": self._dump_mechanisms(),
            "last_external_change_at": _iso(self.last_external_change_at),
            "last_external_change": self.last_external_change,
        }

    def _schedule_save(self) -> None:
        self._store.async_delay_save(self._data_to_save, 5)

    # ------------------------------------------------------------- inputs

    def outdoor_temperature(self, snap: VThermSnapshot | None = None) -> float | None:
        """Outdoor temperature from the configured override sensor, else the VTherm's own."""
        if entity_id := self.option(CONF_OUTDOOR_SENSOR):
            state = self.hass.states.get(entity_id)
            if state is not None:
                try:
                    return float(state.state)
                except (TypeError, ValueError):
                    return None
            return None
        snap = snap or self.snapshot
        return snap.outdoor_temp

    async def _async_seed_if_needed(self) -> None:
        if self.running_mean.t_rm is not None:
            return
        now = dt_util.utcnow()
        today = dt_util.as_local(now).date()
        means: list[float] = []
        if entity_id := self.option(CONF_OUTDOOR_SENSOR):
            means = await async_daily_means(self.hass, entity_id, int(self.const("seed_history_days")))
        current = self.outdoor_temperature()
        seed = seed_running_mean(means, float(self.const("alpha")), fallback=current)
        if seed is None:
            _LOGGER.info("%s: no outdoor reading yet, T_rm seeding deferred", self.room_name)
            return
        self.tracker.seed(float(seed), today, now.isoformat())
        source = "recorder" if means else "current"
        _LOGGER.info("%s: seeded running mean at %.2f C from %s", self.room_name, seed, source)
        self._diagnostic("seed", f"T_rm seeded at {seed:.2f} C from {source}")
        self._schedule_save()

    def _sample_outdoor(self, now: datetime) -> None:
        value = self.outdoor_temperature()
        if value is None:
            stale_after = timedelta(minutes=float(self.const("outdoor_stale_min")))
            if self.last_outdoor_seen_at is None or now - self.last_outdoor_seen_at > stale_after:
                if not self.running_mean.frozen:
                    self.tracker.freeze()
                if not self._outdoor_frozen_reported:
                    self._outdoor_frozen_reported = True
                    self._diagnostic("outdoor_unavailable", "Outdoor temperature unavailable; running mean frozen")
            return
        self.last_outdoor = value
        self.last_outdoor_seen_at = now
        self._outdoor_frozen_reported = False
        if self.running_mean.t_rm is None:
            self.tracker.seed(value, dt_util.as_local(now).date(), now.isoformat())
            self._diagnostic("seed", f"T_rm seeded at {value:.2f} C from current reading")
        interval = timedelta(minutes=float(self.const("outdoor_sample_interval_min")))
        if self.last_outdoor_sample_at is not None and now - self.last_outdoor_sample_at < interval:
            return
        self.last_outdoor_sample_at = now
        self.tracker.add_sample(value, dt_util.as_local(now).date())

    def _maybe_roll(self, now: datetime) -> bool:
        local = dt_util.as_local(now)
        roll_at = parse_hhmm(self.const("daily_recompute_time"), "00:10")
        if local.time() < roll_at:
            return False
        return self.tracker.roll(local.date())

    @property
    def seed_hold_active(self) -> bool:
        seeded = _dt(self.running_mean.seeded_at)
        if seeded is None:
            return True
        return dt_util.utcnow() - seeded < timedelta(hours=float(self.const("seed_hold_hours")))

    # ------------------------------------------------------------- events

    @callback
    def _on_tick(self, _now) -> None:
        self.hass.async_create_task(self.async_evaluate("tick"))

    @callback
    def _on_outdoor_event(self, _event: Event[EventStateChangedData]) -> None:
        self.hass.async_create_task(self.async_evaluate("outdoor"))

    @callback
    def _on_vtherm_event(self, event: Event[EventStateChangedData]) -> None:
        old = event.data.get("old_state")
        new = event.data.get("new_state")
        self._on_vtherm_change(old, new)
        self.hass.async_create_task(self.async_evaluate("vtherm"))

    def _on_vtherm_change(self, old, new) -> None:
        """Note preset changes Hearth did not make. Phase 4 turns these into overrides."""
        if old is None or new is None:
            return
        old_preset = old.attributes.get("preset_mode")
        new_preset = new.attributes.get("preset_mode")
        if old_preset == new_preset or new_preset is None:
            return
        now = dt_util.utcnow()
        window = timedelta(seconds=float(self.const("write_match_window_s")))
        if self.write_log.made_by_us(self.vtherm.entity_id, new_preset, now, window):
            return
        self.last_external_change_at = now
        self.last_external_change = {"kind": "preset", "from": old_preset, "to": new_preset, "at": now.isoformat()}
        self._on_external_change(old, new, old_preset, new_preset, now)

    def _on_external_change(self, old, new, old_preset, new_preset, now: datetime) -> None:
        """Phase 4 hook."""

    # ------------------------------------------------------------- evaluate

    async def async_evaluate(self, reason: str = "manual", *, force_write: bool = False) -> None:
        """Recompute everything from state and apply any writes that are due."""
        if self._evaluating:
            self._pending_evaluate = True
            return
        self._evaluating = True
        try:
            await self._async_evaluate(reason, force_write)
        finally:
            self._evaluating = False
        if self._pending_evaluate:
            self._pending_evaluate = False
            await self.async_evaluate("pending")

    async def _async_evaluate(self, reason: str, force_write: bool) -> None:
        now = dt_util.utcnow()
        self.snapshot = self.vtherm.snapshot()
        self._sample_outdoor(now)
        self._maybe_roll(now)
        self._update_dormant()
        await self._evaluate_adaptive(now, force_write)
        await self._evaluate_mechanisms(now, reason)
        self.last_evaluated = now
        self._schedule_save()
        async_dispatcher_send(self.hass, SIGNAL_ROOM_UPDATE, self.entry_id)

    async def _evaluate_mechanisms(self, now: datetime, reason: str) -> None:
        await self._evaluate_skip(now)
        await self._evaluate_setback(now)

    def _update_dormant(self) -> None:
        if not self.global_active:
            self.dormant_reason = DORMANT_DISABLED
            return
        reason = self.snapshot.dormant_reason
        if reason is None and not self.vtherm.has_preset_numbers(self.affected_presets):
            reason = DORMANT_NO_PRESET_ENTITIES
            if not self._missing_presets_reported:
                self._missing_presets_reported = True
                self._diagnostic(
                    "no_preset_entities",
                    "VTherm preset temperature number entities not found. Untick 'use central configuration' "
                    "for preset temperatures on this VTherm so it exposes number.<vtherm>_preset_comfort_temp.",
                )
        elif reason is None:
            self._missing_presets_reported = False
        self.dormant_reason = reason

    @property
    def dormant(self) -> bool:
        return self.dormant_reason is not None

    @property
    def standdown_active(self) -> bool:
        """Phase 4 overrides this; until then Hearth never stands down."""
        return False

    def learned_target_offset(self) -> float:
        """Phase 4 supplies the learned correction when application is enabled."""
        return 0.0

    async def _evaluate_adaptive(self, now: datetime, force_write: bool) -> None:
        lo, hi = self.band
        hold = self.seed_hold_active or self.running_mean.frozen
        self.comfort = comfort_target(
            float(self.option(CONF_BASE_TEMP)),
            self.number(NUMBER_SLOPE),
            self.running_mean.t_rm,
            float(self.const("t_ref")),
            lo,
            hi,
            self.number(NUMBER_MAX_OFFSET),
            float(self.const("quantise_step")),
            hold=hold,
            extra_offset=self.learned_target_offset(),
        )
        if not self.enabled(SWITCH_ADAPTIVE) or self.dormant or self.standdown_active:
            return
        for preset in self.affected_presets:
            base = float(self.option(CONF_BASE_TEMP)) if preset == PRESET_COMFORT else float(self.option(CONF_BOOST_BASE_TEMP))
            result = (
                self.comfort
                if preset == PRESET_COMFORT
                else comfort_target(
                    base,
                    self.number(NUMBER_SLOPE),
                    self.running_mean.t_rm,
                    float(self.const("t_ref")),
                    lo,
                    hi,
                    self.number(NUMBER_MAX_OFFSET),
                    float(self.const("quantise_step")),
                    hold=hold,
                    extra_offset=self.learned_target_offset(),
                )
            )
            await self._maybe_write_preset_temp(preset, result.quantised, lo, hi, now, force_write)

    async def _maybe_write_preset_temp(self, preset: str, desired: float, lo: float, hi: float, now: datetime, force: bool) -> bool:
        """Write only when the desired quantised value changed since our last write (idempotent)."""
        current = self.vtherm.preset_temp(preset)
        if current is None:
            return False
        last = self.last_written.get(preset)
        if not force:
            if last is not None and abs(last - desired) < 0.01:
                return False  # nothing new to say; a manual edit since then stands
            if abs(current - desired) < 0.01:
                self.last_written[preset] = desired
                return False  # already there (restart, or someone set it for us)
        return await self.async_write_preset_temp(preset, desired, lo, hi, now, "adaptive")

    async def async_write_preset_temp(self, preset: str, value: float, lo: float, hi: float, now: datetime, reason: str) -> bool:
        """The single write path for preset temperatures. Records the write before making it."""
        try:
            entity_id = self.vtherm.preset_number_entity(preset)
            if entity_id is None:
                raise ClampViolation(f"no number entity for preset {preset}")
            self.write_log.record(entity_id, value, now, reason)
            await self.vtherm.write_preset_temp(preset, value, lo, hi)
        except ClampViolation as err:
            _LOGGER.error("%s: write refused: %s", self.room_name, err)
            self._diagnostic("clamp_violation", str(err))
            return False
        except Exception as err:  # noqa: BLE001 - a failed service call must never take the controller down
            _LOGGER.warning("%s: writing %s=%s failed: %s", self.room_name, preset, value, err)
            return False
        self.last_written[preset] = value
        self.last_write = {"preset": preset, "value": value, "at": now.isoformat(), "reason": reason}
        _LOGGER.info("%s: wrote %s preset temp %.1f C (%s)", self.room_name, preset, value, reason)
        return True

    async def async_set_preset(self, preset: str, now: datetime, reason: str) -> bool:
        """The single write path for preset switches. Records the write before making it."""
        self.write_log.record(self.vtherm.entity_id, preset, now, reason)
        try:
            await self.vtherm.set_preset(preset)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("%s: setting preset %s failed: %s", self.room_name, preset, err)
            return False
        self.last_write = {"preset": preset, "value": preset, "at": now.isoformat(), "reason": reason}
        _LOGGER.info("%s: switched preset to %s (%s)", self.room_name, preset, reason)
        return True

    # ------------------------------------------------------------- services

    async def async_service_override(self, minutes: float | None) -> None:
        """hearth.override: stand down deliberately. Phase 4 implements the stand-down itself."""
        raise ServiceValidationError("hearth.override needs the override mechanism (phase 4)")

    async def async_service_set_schedule(self, schedule: dict) -> None:
        raise ServiceValidationError("hearth.set_schedule needs the schedule mechanism (phase 3)")

    async def async_service_reset_learning(self, bucket: str | None) -> None:
        raise ServiceValidationError("hearth.reset_learning needs the learning mechanism (phase 4)")

    # ------------------------------------------------------------- diagnostics

    def _diagnostic(self, kind: str, message: str) -> None:
        _LOGGER.info("%s: [%s] %s", self.room_name, kind, message)
        self.hass.bus.async_fire(
            EVENT_DIAGNOSTIC,
            {"entry_id": self.entry_id, "room": self.room_name, "vtherm": self.vtherm.entity_id, "kind": kind, "message": message},
        )

    def diagnostics(self) -> dict:
        return {
            "room": self.room_name,
            "vtherm": self.vtherm.entity_id,
            "dormant_reason": self.dormant_reason,
            "settings": self.settings,
            "running_mean": self.running_mean.to_dict(),
            "comfort": self.comfort.__dict__ if self.comfort else None,
            "last_written": self.last_written,
            "last_write": self.last_write,
            "write_log": self.write_log.to_list()[-20:],
            "mechanisms": self._dump_mechanisms(),
            "last_external_change": self.last_external_change,
            "forecast_fresh": self.forecast_fresh(dt_util.utcnow()),
        }


def _dt(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = dt_util.parse_datetime(value)
    if parsed is not None and parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt_util.UTC)
    return parsed


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None
