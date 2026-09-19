"""Override detection, stand-down and the learning ledger (spec 4.6).

Recording (the ledger) and stand-down sit behind the `learning` switch.
Application of corrections sits behind the separate `apply_learning` switch.
`hearth.override` stands a room down regardless of either switch.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from homeassistant.exceptions import ServiceValidationError
from homeassistant.util import dt as dt_util

from .const import PRESET_NONE, SWITCH_APPLY_LEARNING, SWITCH_LEARNING
from .core.override import (
    BUCKET_BASELINE,
    BUCKET_PREHEAT,
    BUCKET_SKIP,
    BUCKET_SLOPE,
    BUCKETS,
    Ledger,
    LedgerEntry,
    OverrideContext,
    StandDown,
    attribute,
    qualifies,
    standdown_until,
)
from .vtherm import snapshot_from_state

_LOGGER = logging.getLogger(__name__)


class LearningMixin:
    """Mixed into HearthRoom, first in the MRO so its hooks win."""

    standdown: StandDown
    ledger: Ledger

    def _init_learning(self) -> None:
        self.standdown = StandDown()
        self.ledger = Ledger()

    @property
    def learning(self) -> dict[str, Any]:
        return self.mechanisms.setdefault("learning", {"ignored": []})

    def _load_learning_state(self) -> None:
        self.standdown = StandDown.from_dict(self.mechanisms.get("standdown"))
        self.ledger = Ledger.from_dict(self.mechanisms.get("ledger"))

    def _dump_learning_state(self) -> None:
        self.mechanisms["standdown"] = self.standdown.to_dict()
        self.mechanisms["ledger"] = self.ledger.to_dict()

    @property
    def half_life(self) -> timedelta:
        return timedelta(days=float(self.const("learning_half_life_days")))

    # ------------------------------------------------------------- stand-down

    @property
    def standdown_active(self) -> bool:
        return self.standdown.active(dt_util.utcnow())

    def _start_standdown(self, now: datetime, cause: str, minutes: float | None = None) -> None:
        timeout = timedelta(minutes=float(minutes if minutes is not None else self.const("standdown_timeout_min")))
        boundary = None if minutes is not None else self.next_boundary(now)
        self.standdown = StandDown(until=standdown_until(now, boundary, timeout), cause=cause, started_at=now)
        self._diagnostic("standdown", f"Standing down until {dt_util.as_local(self.standdown.until):%H:%M} ({cause})")

    # ------------------------------------------------------------- detection

    def _on_vtherm_change(self, old, new) -> None:
        """Detect preset and target changes Hearth did not make."""
        if old is None or new is None:
            return
        # Judge the change against the state it produced, not the last evaluated snapshot.
        self.snapshot = snapshot_from_state(new)
        self._update_dormant()
        now = dt_util.utcnow()
        window = timedelta(seconds=float(self.const("write_match_window_s")))
        old_preset = old.attributes.get("preset_mode")
        new_preset = new.attributes.get("preset_mode")
        old_target = _f(old.attributes.get("temperature"))
        new_target = _f(new.attributes.get("temperature"))
        if new_preset is not None and new_preset != old_preset:
            if self.write_log.made_by_us(self.vtherm.entity_id, new_preset, now, window):
                return
            magnitude = None
            if new_preset == PRESET_NONE and old_target is not None and new_target is not None:
                kind, magnitude = "setpoint", new_target - old_target
            else:
                kind = "preset"
                old_temp = self.vtherm.preset_temp(old_preset) if old_preset else None
                new_temp = self.vtherm.preset_temp(new_preset)
                if old_temp is not None and new_temp is not None:
                    magnitude = new_temp - old_temp
                elif old_target is not None and new_target is not None:
                    magnitude = new_target - old_target
            self.last_external_change_at = now
            self.last_external_change = {"kind": kind, "from": old_preset, "to": new_preset, "magnitude": magnitude, "at": now.isoformat()}
            self._handle_override(now, kind, old_preset, new_preset, magnitude)
            return
        if old_target is not None and new_target is not None and abs(new_target - old_target) > 0.01:
            # Same preset, target moved: a preset-temp number edit (ours, or someone's) or a manual setpoint.
            for preset in (new_preset,) if new_preset else ():
                entity_id = self.vtherm.preset_number_entity(preset)
                if entity_id and self.write_log.made_by_us(entity_id, new_target, now, window):
                    return
            magnitude = new_target - old_target
            self.last_external_change_at = now
            self.last_external_change = {
                "kind": "temp",
                "from": old_target,
                "to": new_target,
                "magnitude": magnitude,
                "at": now.isoformat(),
            }
            self._handle_override(now, "temp", new_preset, new_preset, magnitude)

    def _handle_override(self, now: datetime, kind: str, old_preset: str | None, new_preset: str | None, magnitude: float | None) -> None:
        if not self.enabled(SWITCH_LEARNING):
            return
        if self.dormant:
            self._ignore_override(now, kind, "dormant")
            return
        affected = tuple(self.affected_presets)
        if old_preset not in affected and not (kind == "preset" and new_preset in affected):
            self._ignore_override(now, kind, "unaffected_preset")
            return
        self._start_standdown(now, f"manual_{kind}")
        self._record_override(now, kind, old_preset, new_preset, magnitude)

    def _record_override(self, now: datetime, kind: str, old_preset: str | None, new_preset: str | None, magnitude: float | None) -> None:
        if magnitude is None or abs(magnitude) < 0.01:
            self._ignore_override(now, kind, "no_magnitude")
            return
        direction = 1 if magnitude > 0 else -1
        last_q = self.ledger.last_entry_at()
        schedule_on = self.enabled("schedule") and not self.schedule_model.is_empty
        recent_wb = self.schedule_model.recent_warm_by(now, self.tz, timedelta(hours=6)) if schedule_on else None
        ctx = OverrideContext(
            dormant=self.dormant,
            forecast_stale=not self.forecast_fresh(now),
            active_preset=old_preset,
            affected_presets=tuple(self.affected_presets),
            skip_state=self.skip.get("status", "idle"),
            skip_ended_minutes_ago=self.skip_ended_minutes_ago,
            minutes_since_warm_by=((now - recent_wb.start).total_seconds() / 60) if recent_wb else None,
            t_rm=self.running_mean.t_rm,
            t_ref=float(self.const("t_ref")),
            cold_rm_margin=float(self.const("slope_error_cold_margin")),
            last_qualifying_minutes_ago=((now - last_q).total_seconds() / 60) if last_q else None,
            storm_window_min=float(self.const("override_storm_window_min")),
        )
        exclusion = qualifies(ctx, new_preset=new_preset if kind == "preset" else None)
        if exclusion:
            self._ignore_override(now, kind, exclusion)
            return
        bucket, weight = attribute(
            ctx,
            direction,
            preheat_window_min=float(self.const("preheat_shortfall_window_min")),
            skip_after_window_min=float(self.const("skip_ended_learning_window_min")),
        )
        bound = float(self.const("learning_target_bound"))
        capped = min(abs(magnitude), 2 * bound)
        last_preset_write = self.write_log.last_for(self.vtherm.entity_id)
        entry = LedgerEntry(
            at=now,
            direction=direction,
            magnitude=capped,
            t_rm=self.running_mean.t_rm,
            t_out=self.outdoor_temperature(),
            minutes_since_preset_change=((now - last_preset_write.at).total_seconds() / 60) if last_preset_write else None,
            skip_state=ctx.skip_state,
            applied_offset=self.comfort.offset_applied if self.comfort else 0.0,
            active_preset=old_preset,
            bucket=bucket,
            weight=weight,
            kind=kind,
        )
        self.ledger.add(entry, self.half_life)
        self.learning["last_override"] = {**entry.to_dict(), "raw_magnitude": magnitude}
        self._diagnostic("override", f"Override recorded: {kind} {'+' if direction > 0 else '-'}{capped:.1f} C -> {bucket}")

    def _ignore_override(self, now: datetime, kind: str, reason: str) -> None:
        ignored = self.learning.setdefault("ignored", [])
        ignored.append({"at": now.isoformat(), "kind": kind, "reason": reason})
        del ignored[:-20]

    # ------------------------------------------------------------- application

    def _correction(self, bucket: str) -> float:
        stats = self.ledger.buckets.get(bucket)
        if stats is None:
            return 0.0
        return stats.correction(
            dt_util.utcnow(),
            self.half_life,
            min_events=float(self.const("learning_min_events")),
            bound=float(self.const("learning_target_bound")),
        )

    def learned_target_offset(self) -> float:
        if not self.enabled(SWITCH_APPLY_LEARNING):
            return 0.0
        total = self._correction(BUCKET_BASELINE)
        t_rm = self.running_mean.t_rm
        if t_rm is not None and t_rm < float(self.const("t_ref")) - float(self.const("slope_error_cold_margin")):
            total += self._correction(BUCKET_SLOPE)
        bound = float(self.const("learning_target_bound"))
        return max(-bound, min(bound, total))

    def learned_skip_threshold_offset(self) -> float:
        if not self.enabled(SWITCH_APPLY_LEARNING):
            return 0.0
        return self._correction(BUCKET_SKIP)

    def learned_safety_factor_multiplier(self) -> float:
        if not self.enabled(SWITCH_APPLY_LEARNING):
            return 1.0
        pct = float(self.const("learning_safety_factor_bound_pct")) / 100.0
        c = self._correction(BUCKET_PREHEAT) / float(self.const("learning_target_bound"))
        return 1.0 + max(-1.0, min(1.0, c)) * pct

    def learning_summary(self, bucket: str) -> dict[str, Any]:
        stats = self.ledger.buckets.get(bucket)
        now = dt_util.utcnow()
        if stats is None:
            return {}
        return {
            "bias": round(stats.mean, 3),
            "evidence": round(stats.evidence(now, self.half_life), 2),
            "events": stats.events,
            "positive_weight": round(stats.pos, 2),
            "negative_weight": round(stats.neg, 2),
            "last_event_at": stats.last_at.isoformat() if stats.last_at else None,
            "would_apply": self._correction(bucket),
            "applying": self.enabled(SWITCH_APPLY_LEARNING),
            "recording": self.enabled(SWITCH_LEARNING),
            "half_life_days": self.const("learning_half_life_days"),
            "min_events": self.const("learning_min_events"),
        }

    # ------------------------------------------------------------- services

    async def async_service_override(self, minutes: float | None) -> None:
        now = dt_util.utcnow()
        self._start_standdown(now, "service", minutes)
        if self.skip_active:
            await self._end_skip(now, "override_service", restore=True)
        self._schedule_save()
        await self.async_evaluate("override_service")

    async def async_service_reset_learning(self, bucket: str | None) -> None:
        if bucket is not None and bucket not in BUCKETS:
            raise ServiceValidationError(f"Unknown bucket {bucket!r}; expected one of {', '.join(BUCKETS)}")
        self.ledger.reset(bucket)
        self._diagnostic("reset_learning", f"Ledger reset ({bucket or 'all buckets'})")
        self._schedule_save()
        await self.async_evaluate("reset_learning")


def _f(value) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
