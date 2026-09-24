"""Forecast skip and skip preview (spec 4.2)."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any

from homeassistant.util import dt as dt_util

from .const import (
    CONF_SKIP_DECISION_TIME,
    CONF_SKIP_END_TIME,
    CONF_SOLAR_GAIN,
    NUMBER_MIN_INDOOR_FLOOR,
    NUMBER_SKIP_THRESHOLD,
    PRESET_ECO,
    PRESET_FROST,
    SKIP_ABORTED,
    SKIP_ACTIVE,
    SKIP_IDLE,
    SKIP_PREVIEW,
    SWITCH_SKIP,
    TIER2_DEFAULTS,
)
from .core.forecast import ForecastCache, SkipDecision, decide_skip, derived_base_threshold, skip_abort_reason

_LOGGER = logging.getLogger(__name__)

ABORT_REASONS = ("below_indoor_floor", "manual_intervention", "dormant", "forecast_shortfall", "disabled")


def local_at(day: date, hhmm: str, fallback: str) -> datetime:
    """Aware local datetime for `day` at HH:MM."""
    from .room import parse_hhmm  # local import to avoid a cycle

    t = parse_hhmm(hhmm, fallback)
    return datetime.combine(day, t, tzinfo=dt_util.get_default_time_zone())


class SkipMixin:
    """Skip decision, active-skip supervision, preview. Mixed into HearthRoom."""

    # State lives in self.mechanisms["skip"] and ["preview"] (persisted dicts).

    @property
    def skip(self) -> dict[str, Any]:
        return self.mechanisms.setdefault("skip", {"status": SKIP_IDLE})

    @property
    def preview(self) -> dict[str, Any]:
        return self.mechanisms.setdefault("preview", {})

    # ------------------------------------------------------------- forecast helpers

    def forecast_cache(self) -> ForecastCache:
        return self.hass.data["hearth"]["forecast"].cache(self.weather_entity_id)

    def forecast_fresh(self, now: datetime) -> bool:
        return self.forecast_cache().is_fresh(now, timedelta(hours=float(self.const("max_forecast_age_hours"))))

    def _day_bounds(self, day: date) -> tuple[datetime, datetime]:
        start = local_at(day, "00:00", "00:00")
        return start, start + timedelta(days=1)

    def forecast_high_for(self, day: date) -> float | None:
        cache = self.forecast_cache()
        start, end = self._day_bounds(day)
        cutoff = local_at(day, self.const("skip_hourly_cutoff"), "14:00") if self.const("skip_hourly_mode") else None
        return cache.high_for_day(day, start, end, cutoff)

    def forecast_condition_for(self, day: date) -> str | None:
        cache = self.forecast_cache()
        return cache.condition_for_day(
            day,
            local_at(day, self.const("forecast_condition_window_start"), "08:00"),
            local_at(day, self.const("forecast_condition_window_end"), "14:00"),
        )

    def effective_skip_threshold(self) -> float:
        base = self.number(NUMBER_SKIP_THRESHOLD)
        if self.const("skip_threshold_from_trm"):
            base = derived_base_threshold(
                base, self.running_mean.t_rm, float(self.const("t_ref")), float(self.const("skip_threshold_trm_factor"))
            )
        return base + self.learned_skip_threshold_offset()

    def learned_skip_threshold_offset(self) -> float:
        """Phase 4 supplies the learned correction when application is enabled."""
        return 0.0

    def _skip_decision_for(self, day: date, now: datetime, *, in_scope: bool, indoor: float | None) -> SkipDecision:
        return decide_skip(
            forecast_fresh=self.forecast_fresh(now),
            forecast_high=self.forecast_high_for(day),
            condition=self.forecast_condition_for(day),
            indoor_temp=indoor,
            min_indoor_floor=self.number(NUMBER_MIN_INDOOR_FLOOR),
            base_threshold=self.effective_skip_threshold(),
            solar_room=bool(self.option(CONF_SOLAR_GAIN)),
            solar_boost=float(self.const("solar_boost")),
            in_scope=in_scope,
        )

    # ------------------------------------------------------------- times

    def skip_decision_time(self, day: date) -> datetime:
        return local_at(day, self.option(CONF_SKIP_DECISION_TIME), TIER2_DEFAULTS[CONF_SKIP_DECISION_TIME])

    def skip_end_time(self, day: date) -> datetime:
        return local_at(day, self.option(CONF_SKIP_END_TIME), TIER2_DEFAULTS[CONF_SKIP_END_TIME])

    # ------------------------------------------------------------- evaluation

    async def _evaluate_skip(self, now: datetime) -> None:
        local = dt_util.as_local(now)
        today = local.date()
        skip = self.skip
        if skip.get("status") == SKIP_ACTIVE:
            await self._supervise_skip(now, today)
        elif skip.get("decided_date") != today.isoformat() and now >= self.skip_decision_time(today):
            await self._decide_skip(now, today)
        self._evaluate_preview(now, today)

    async def _decide_skip(self, now: datetime, today: date) -> None:
        skip = self.skip
        decision_time = self.skip_decision_time(today)
        late_window = timedelta(minutes=float(self.const("skip_late_decision_window_min")))
        if now > decision_time + late_window or now >= self.skip_end_time(today):
            self._mark_skip_decided(today, SkipDecision(False, "missed"), now)
            return
        in_scope = self.enabled(SWITCH_SKIP) and not self.dormant and not self.standdown_active
        decision = self._skip_decision_for(today, now, in_scope=in_scope, indoor=self.snapshot.current_temp)
        if decision.reason == "forecast_stale":
            # Fail toward heating normally, but keep trying for the late window in case a fetch lands.
            skip.update({"pending_reason": "forecast_stale"})
            return
        if decision.skip and self.snapshot.preset == PRESET_FROST:
            # Switching frost to eco would add heat, not save it.
            decision = SkipDecision(False, "in_frost", decision.threshold, decision.forecast_high, decision.condition)
        self._mark_skip_decided(today, decision, now)
        if not decision.skip:
            return
        previous = self.snapshot.preset
        skip.update(
            {
                "status": SKIP_ACTIVE,
                "date": today.isoformat(),
                "previous_preset": previous,
                "started_at": now.isoformat(),
                "ends_at": self.skip_end_time(today).isoformat(),
                "indoor_at_start": self.snapshot.current_temp,
                "recheck_done": False,
                "aborted_reason": None,
                "ended_at": None,
            }
        )
        if previous != PRESET_ECO:
            await self.async_set_preset(PRESET_ECO, now, "skip")
        self._diagnostic("skip_start", f"Skipping morning heat: forecast high {decision.forecast_high} >= {decision.threshold}")

    def _mark_skip_decided(self, today: date, decision: SkipDecision, now: datetime) -> None:
        self.skip.update(
            {
                "status": SKIP_IDLE if self.skip.get("status") != SKIP_ABORTED else SKIP_ABORTED,
                "decided_date": today.isoformat(),
                "decided_at": now.isoformat(),
                "reason": decision.reason,
                "threshold": decision.threshold,
                "forecast_high": decision.forecast_high,
                "condition": decision.condition,
                "pending_reason": None,
            }
        )
        if decision.skip or self.skip.get("date") != today.isoformat():
            self.skip["status"] = SKIP_IDLE
            self.skip["aborted_reason"] = None

    async def _supervise_skip(self, now: datetime, today: date) -> None:
        skip = self.skip
        if not self.enabled(SWITCH_SKIP):
            await self._end_skip(now, "disabled", restore=True)
            return
        recheck_at = local_at(today, self.const("skip_recheck_time"), "11:00")
        recheck_due = now >= recheck_at and not skip.get("recheck_done")
        outdoor_forecast = self.forecast_cache().temperature_at(now) if recheck_due else None
        reason = skip_abort_reason(
            indoor_temp=self.snapshot.current_temp,
            min_indoor_floor=self.number(NUMBER_MIN_INDOOR_FLOOR),
            recheck_due=recheck_due,
            indoor_at_start=skip.get("indoor_at_start"),
            outdoor_actual=self.outdoor_temperature(),
            outdoor_forecast=outdoor_forecast,
            outdoor_shortfall=float(self.const("skip_abort_outdoor_shortfall")),
            manual_intervention=self._manual_change_since(_dt(skip.get("started_at"))),
            dormant=self.dormant,
        )
        if recheck_due:
            skip["recheck_done"] = True
        if reason is not None:
            await self._end_skip(now, reason, restore=reason != "manual_intervention")
            return
        if self.schedule_ends_skip():
            await self._end_skip(now, "non_skippable_block", restore=True)
            return
        ends_at = _dt(skip.get("ends_at"))
        if ends_at is not None and now >= ends_at:
            await self._end_skip(now, "ended", restore=True)

    async def _end_skip(self, now: datetime, reason: str, *, restore: bool) -> None:
        skip = self.skip
        previous = skip.get("previous_preset")
        target = self.restore_preset_for_skip(previous)
        if restore and self.snapshot.preset == PRESET_ECO and target and target != PRESET_ECO:
            await self.async_set_preset(target, now, f"skip_{reason}")
        skip.update(
            {
                "status": SKIP_ABORTED if reason in ABORT_REASONS else SKIP_IDLE,
                "aborted_reason": reason if reason in ABORT_REASONS else None,
                "end_reason": reason,
                "ended_at": now.isoformat(),
            }
        )
        self._diagnostic("skip_end", f"Skip ended: {reason}")

    def _manual_change_since(self, since: datetime | None) -> bool:
        """A preset change Hearth did not make, after `since`."""
        last = self.last_external_change_at
        return since is not None and last is not None and last > since

    @property
    def skip_active(self) -> bool:
        return self.skip.get("status") == SKIP_ACTIVE

    @property
    def skip_ended_minutes_ago(self) -> float | None:
        ended = _dt(self.skip.get("ended_at"))
        if ended is None:
            return None
        return (dt_util.utcnow() - ended).total_seconds() / 60

    # ------------------------------------------------------------- preview

    def _evaluate_preview(self, now: datetime, today: date) -> None:
        """Preview never commits. Before today's decision time it previews today, afterwards tomorrow."""
        day = today if now < self.skip_decision_time(today) else today + timedelta(days=1)
        decision = self._skip_decision_for(day, now, in_scope=True, indoor=self.snapshot.current_temp if day == today else None)
        if decision.reason in ("no_indoor_temp", "below_indoor_floor") and day != today:
            # Indoor temp tomorrow morning is unknown; judge the forecast alone.
            decision = self._skip_decision_for(day, now, in_scope=True, indoor=self.number(NUMBER_MIN_INDOOR_FLOOR))
        likely: bool | None
        if decision.reason in ("forecast_stale", "no_forecast_high"):
            likely = None
        else:
            likely = decision.skip
        self.preview.update(
            {
                "for_date": day.isoformat(),
                "likely": likely,
                "reason": decision.reason,
                "threshold": decision.threshold,
                "forecast_high": decision.forecast_high,
                "condition": decision.condition,
                "computed_at": now.isoformat(),
                "skip_enabled": self.enabled(SWITCH_SKIP),
            }
        )

    @property
    def skip_status(self) -> str:
        skip = self.skip
        status = skip.get("status", SKIP_IDLE)
        if status in (SKIP_ACTIVE, SKIP_ABORTED):
            return status
        if self.preview.get("likely") and self.enabled(SWITCH_SKIP):
            return SKIP_PREVIEW
        return SKIP_IDLE


def _dt(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = dt_util.parse_datetime(value)
    if parsed is not None and parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt_util.UTC)
    return parsed
