"""Warm-by schedule, preheat and the warming-rate learner (spec 4.4, 4.5)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from homeassistant.core import CALLBACK_TYPE, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.util import dt as dt_util

from .const import CONF_SCHEDULE, CONF_SOLAR_GAIN, CONF_WORKDAY, PRESET_ECO, SWITCH_PREHEAT, SWITCH_SCHEDULE
from .core.forecast import SUNNY_CONDITIONS
from .core.preheat import HeatingRun, WarmingModel, lead_minutes, preheat_start, warming_rate
from .core.schedule import BlockInstance, Schedule, ScheduleError

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class PreheatPlan:
    """The plan for the next warm-by block."""

    block: BlockInstance
    target: float
    deficit: float
    rate: float
    learned: bool
    lead_min: float
    start: datetime
    t_out: float | None
    solar_discount: float


class ScheduleMixin:
    """Preset switching from the schedule, preheat, learner. Mixed into HearthRoom."""

    schedule_model: Schedule
    warming_model: WarmingModel
    heating_run: HeatingRun | None
    preheat_plan: PreheatPlan | None
    _schedule_timer: CALLBACK_TYPE | None
    _schedule_timer_at: datetime | None

    def _init_schedule(self) -> None:
        self.schedule_model = Schedule({})
        self.warming_model = WarmingModel()
        self.heating_run = None
        self.preheat_plan = None
        self._schedule_timer = None
        self._schedule_timer_at = None

    @property
    def sched(self) -> dict[str, Any]:
        return self.mechanisms.setdefault("schedule", {})

    def _load_schedule_state(self) -> None:
        try:
            self.schedule_model = Schedule.parse(self.option(CONF_SCHEDULE))
        except ScheduleError as err:
            _LOGGER.error("%s: invalid schedule in options ignored: %s", self.room_name, err)
            self.schedule_model = Schedule({})
        self.warming_model = WarmingModel.from_dict(self.mechanisms.get("warming"))
        self.warming_model.max_runs = int(self.const("max_learning_runs"))
        self.heating_run = HeatingRun.from_dict(self.mechanisms.get("run"))

    def _dump_schedule_state(self) -> None:
        self.mechanisms["warming"] = self.warming_model.to_dict()
        self.mechanisms["run"] = self.heating_run.to_dict() if self.heating_run else None

    def _stop_schedule_timer(self) -> None:
        if self._schedule_timer is not None:
            self._schedule_timer()
            self._schedule_timer = None
            self._schedule_timer_at = None

    # ------------------------------------------------------------- planning

    @property
    def tz(self):
        return dt_util.get_default_time_zone()

    def current_block(self, now: datetime) -> BlockInstance | None:
        return self.schedule_model.current(now, self.tz)

    def next_block(self, now: datetime) -> BlockInstance | None:
        return self.schedule_model.next(now, self.tz)

    def next_boundary(self, now: datetime) -> datetime | None:
        if self.schedule_model.is_empty or not self.enabled(SWITCH_SCHEDULE):
            return None
        return self.schedule_model.next_boundary(now, self.tz)

    def learned_safety_factor_multiplier(self) -> float:
        """Phase 4 supplies the learned multiplier when application is enabled."""
        return 1.0

    def preheat_target_for(self, preset: str) -> float | None:
        value = self.vtherm.preset_temp(preset)
        if value is None and preset == "comfort" and self.comfort is not None:
            value = self.comfort.quantised
        return value

    def _plan_preheat(self, now: datetime) -> PreheatPlan | None:
        block = self.schedule_model.next_warm_by(now, self.tz)
        if block is None:
            return None
        if block.start - now > timedelta(hours=float(self.const("preheat_lookahead_hours"))):
            return None
        target = self.preheat_target_for(block.preset)
        current = self.snapshot.current_temp
        if target is None or current is None:
            return None
        discount = 0.0
        if self.option(CONF_SOLAR_GAIN) and self.forecast_condition_for(dt_util.as_local(block.start).date()) in SUNNY_CONDITIONS:
            discount = float(self.const("solar_gain_discount"))
        target -= discount
        deficit = target - current
        t_out = self.forecast_cache().temperature_at(block.start - timedelta(hours=1))
        if t_out is None:
            t_out = self.outdoor_temperature()
        rate, learned = warming_rate(
            self.warming_model,
            target,
            t_out,
            min_runs=int(self.const("min_learning_runs")),
            default_rate=float(self.const("default_rate")),
            rate_min=float(self.const("rate_min")),
            rate_max=float(self.const("rate_max")),
        )
        safety = float(self.const("safety_factor")) * self.learned_safety_factor_multiplier()
        lead = lead_minutes(deficit, rate, safety_factor=safety, max_preheat_min=float(self.const("max_preheat_min")))
        return PreheatPlan(block, target, deficit, rate, learned, lead, preheat_start(block.start, lead), t_out, discount)

    # ------------------------------------------------------------- evaluation

    async def _refresh_workdays(self, now: datetime) -> None:
        """Fill schedule_model.non_workdays from the optional workday sensor.

        Uses `workday.check_date` for yesterday to two days ahead, cached per date in
        the persisted schedule state. A failed lookup is not cached (retried next tick);
        today falls back to the sensor's own state. Unknown dates follow their weekday.
        """
        entity_id = self.option(CONF_WORKDAY)
        if not entity_id:
            self.schedule_model.non_workdays = set()
            return
        today = dt_util.as_local(now).date()
        wanted = [today + timedelta(days=d) for d in (-1, 0, 1, 2)]
        cache: dict[str, bool] = self.sched.setdefault("workdays", {})
        for day in wanted:
            key = day.isoformat()
            if key in cache:
                continue
            try:
                response = await self.hass.services.async_call(
                    "workday", "check_date", {"entity_id": entity_id, "check_date": key}, blocking=True, return_response=True
                )
                cache[key] = bool((response or {})[entity_id]["workday"])
            except Exception as err:  # noqa: BLE001 - a missing or broken workday sensor must not stop the schedule
                _LOGGER.debug("%s: workday lookup for %s failed: %s", self.room_name, key, err)
                if day == today and (state := self.hass.states.get(entity_id)) is not None and state.state in ("on", "off"):
                    cache[key] = state.state == "on"
        for key in [k for k in cache if k < wanted[0].isoformat()]:
            del cache[key]
        self.schedule_model.non_workdays = {date.fromisoformat(k) for k, v in cache.items() if not v}

    async def _evaluate_schedule(self, now: datetime) -> None:
        await self._refresh_workdays(now)
        self._learn(now)
        if self.schedule_model.is_empty:
            self.preheat_plan = None
            self._stop_schedule_timer()
            return
        self.preheat_plan = self._plan_preheat(now)
        current = self.current_block(now)
        desired = current
        reason = "schedule"
        plan = self.preheat_plan
        if self.enabled(SWITCH_SCHEDULE) and self.enabled(SWITCH_PREHEAT) and plan is not None and plan.start <= now < plan.block.start:
            desired = plan.block
            reason = "preheat"
        self._arm_schedule_timer(now)
        if not self.enabled(SWITCH_SCHEDULE) or self.dormant or self.standdown_active or desired is None:
            return
        if self.skip_active:
            return  # the skip owns the preset; it ends on a non-skippable block (schedule_ends_skip)
        if self.sched.get("applied_key") == desired.key:
            return
        external = self.last_external_change_at
        if external is not None and external >= desired.start and self.sched.get("applied_key") is not None:
            # A manual change after this block started stands until the next block.
            self.sched.update({"applied_key": desired.key, "applied_at": now.isoformat(), "applied_by": "manual"})
            return
        if self.snapshot.preset != desired.preset:
            await self.async_set_preset(desired.preset, now, reason)
        self.sched.update({"applied_key": desired.key, "applied_at": now.isoformat(), "applied_by": reason})

    def _arm_schedule_timer(self, now: datetime) -> None:
        """One-shot timer for the next instant the schedule needs to act."""
        candidates = []
        if nxt := self.next_block(now):
            candidates.append(nxt.start)
        if self.preheat_plan is not None and self.preheat_plan.start > now:
            candidates.append(self.preheat_plan.start)
        if not candidates:
            self._stop_schedule_timer()
            return
        at = min(candidates) + timedelta(seconds=2)
        if self._schedule_timer_at == at:
            return
        self._stop_schedule_timer()
        self._schedule_timer_at = at
        self._schedule_timer = async_track_point_in_utc_time(self.hass, self._on_schedule_timer, dt_util.as_utc(at))

    @callback
    def _on_schedule_timer(self, _now) -> None:
        self._schedule_timer = None
        self._schedule_timer_at = None
        self.hass.async_create_task(self.async_evaluate("schedule_timer"))

    def schedule_ends_skip(self) -> bool:
        if not self.enabled(SWITCH_SCHEDULE) or self.schedule_model.is_empty:
            return False
        now = dt_util.utcnow()
        current = self.current_block(now)
        started = _dt(self.skip.get("started_at"))
        if current is None or started is None:
            return False
        return current.start > started and not current.block.skippable and current.preset != PRESET_ECO

    def restore_preset_for_skip(self, previous: str | None) -> str | None:
        if self.enabled(SWITCH_SCHEDULE) and not self.schedule_model.is_empty:
            current = self.current_block(dt_util.utcnow())
            if current is not None:
                self.sched.update({"applied_key": current.key, "applied_at": dt_util.utcnow().isoformat(), "applied_by": "skip_restore"})
                return current.preset
        return previous

    # ------------------------------------------------------------- learner

    def _learn(self, now: datetime) -> None:
        snap = self.snapshot
        clean = (
            snap.heating
            and snap.preset in self.affected_presets
            and not self.dormant
            and not self.standdown_active
            and snap.current_temp is not None
        )
        run = self.heating_run
        if run is None:
            if clean and snap.target_temp is not None:
                self.heating_run = HeatingRun(now, snap.target_temp, snap.current_temp, snap.current_temp, now)
                self.heating_run.sample(now, snap.current_temp, self.outdoor_temperature())
            return
        if self.dormant:
            run.taint(self.dormant_reason or "dormant")
        elif self.standdown_active:
            run.taint("standdown")
        elif self.last_external_change_at is not None and self.last_external_change_at > run.started_at:
            run.taint("manual_change")
        elif snap.target_temp is not None and abs(snap.target_temp - run.t_target) > 0.01:
            run.taint("target_change")
        if snap.heating and snap.current_temp is not None and not run.tainted:
            run.sample(now, snap.current_temp, self.outdoor_temperature())
            return
        result = run.result(min_minutes=float(self.const("learn_min_run_min")), min_rise=float(self.const("learn_min_rise")))
        if result is not None:
            delta, rate = result
            self.warming_model.add_run(delta, rate)
            self._diagnostic("learned_run", f"Clean heating run: {rate:.2f} C/h at delta {delta:.1f} C ({self.warming_model.n_runs} runs)")
        self.heating_run = None

    def current_warming_rate(self) -> tuple[float, bool]:
        target = self.comfort.quantised if self.comfort else float(self.option("base_comfort_temp"))
        return warming_rate(
            self.warming_model,
            target,
            self.outdoor_temperature(),
            min_runs=int(self.const("min_learning_runs")),
            default_rate=float(self.const("default_rate")),
            rate_min=float(self.const("rate_min")),
            rate_max=float(self.const("rate_max")),
        )

    # ------------------------------------------------------------- services

    async def async_service_set_schedule(self, schedule: dict) -> None:
        try:
            parsed = Schedule.parse(schedule)
        except ScheduleError as err:
            raise ServiceValidationError(f"Invalid schedule: {err}") from err
        options = {**self.entry.options, CONF_SCHEDULE: parsed.to_dict()}
        self.hass.config_entries.async_update_entry(self.entry, options=options)


def _dt(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = dt_util.parse_datetime(value)
    if parsed is not None and parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt_util.UTC)
    return parsed
