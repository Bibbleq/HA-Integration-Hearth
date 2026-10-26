"""Override detection, stand-down and the learning ledger (spec 4.6).

Recording (the ledger) and stand-down sit behind the `learning` switch.
Application of corrections sits behind the separate `apply_learning` switch.
`hearth.override` stands a room down regardless of either switch.
"""

from __future__ import annotations

import copy
import logging
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any

from homeassistant.core import CALLBACK_TYPE, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util

from .const import PRESET_BOOST, PRESET_NONE, RESET_WARMING_RATE, SWITCH_APPLY_LEARNING, SWITCH_LEARNING
from .core.boost import NON_WORKDAY, WORKDAY, BoostEvent, prune, suggestions
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

VT_TIMED_PRESET = "timed_preset_manager"
STANDDOWN_TIMED_PRESET = "timed_preset"
BOOST_SOURCES_PERSON = ("person", "dashboard", "valve", "voice")
BOOST_SOURCES = (*BOOST_SOURCES_PERSON, "automation")


def _timed(state) -> dict[str, Any]:
    """VTherm's timed-preset block (`versatile_thermostat.set_timed_preset`), or {} when absent."""
    block = state.attributes.get(VT_TIMED_PRESET) if state is not None else None
    return block if isinstance(block, dict) else {}


ORIGIN_HUMAN = "human"
ORIGIN_AUTOMATION = "automation"
# A preset-number edit by a person reaches the climate entity a moment later without their context (D47)
_NUMBER_CONTEXT_WINDOW_S = 10


class LearningMixin:
    """Mixed into HearthRoom, first in the MRO so its hooks win."""

    standdown: StandDown
    ledger: Ledger

    def _init_learning(self) -> None:
        self._override_undo: dict[str, Any] | None = None
        self.standdown = StandDown()
        self.ledger = Ledger()
        self._settle_marks: dict[str, datetime] = {}
        self._coalesce: dict[str, Any] | None = None  # D67: a person's stepped change being folded into one override
        self._coalesce_timer: CALLBACK_TYPE | None = None
        self._reload_grace_until: datetime | None = None  # D66: the VTherm just came back; its changes are restores

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

    # ------------------------------------------------------------- settle window (D64)

    def _mark_settle(self, kind: str, now: datetime) -> None:
        self._settle_marks[kind] = now

    def _settling(self, now: datetime) -> str | None:
        """D64: the reason a change right now is not learnt from, or None.

        For a while after Hearth writes, wakes from dormancy, starts or ends a preheat, or a boost ends, the VTherm
        and the valves are still catching up, and what looks like a person's change is often the house settling:
        a Tado valve reporting the setpoint it was just sent (Main Bedroom 24 Oct 05:28, "knob" to 25 C five minutes
        after preheat came back on) or one that has not caught up yet (Living Room 24 Oct 13:14, eight minutes after
        waking from Away). The stand-down still happens; only the ledger and boost patterns skip it. Hearth's own
        writes for hearth.boost do not count, or no boost could ever be learnt.
        """
        window = timedelta(minutes=float(self.const("learning_settle_min")))
        marks = dict(self._settle_marks)
        for rec in reversed(self.write_log.records):
            if not rec.reason.startswith("boost"):
                marks["own_write"] = rec.at
                break
        for kind, at in marks.items():
            if timedelta(0) <= now - at < window:
                return f"settling_{kind}"
        return None

    # ------------------------------------------------------------- detection

    def _change_origin(self, new, number_entity: str | None = None) -> str:
        """D47: a change is a person's when its context carries a user, otherwise it came from an automation,
        script, Scheduler or integration. A preset-number edit is judged on the number's own state as well."""
        if self.const("learn_from_automations") or new.context.user_id is not None:
            return ORIGIN_HUMAN
        if number_entity:
            number = self.hass.states.get(number_entity)
            if (
                number is not None
                and number.context.user_id is not None
                and (dt_util.utcnow() - number.last_changed).total_seconds() <= _NUMBER_CONTEXT_WINDOW_S
            ):
                return ORIGIN_HUMAN
        return ORIGIN_AUTOMATION

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
        if self._not_ready(old) and not self._not_ready(new):
            # D66: back from unavailable or initialising (a reload, a dropped connection). For a while its changes
            # are its own restore, not anyone's decision (Main Bedroom 25 Oct 18:46: comfort put back 0.9 s after ready).
            self._reload_grace_until = now + timedelta(seconds=float(self.const("reload_grace_s")))
        startup = self._is_startup_change(old, now)
        old_tm, new_tm = _timed(old), _timed(new)
        if not new_tm.get("is_active"):
            self._maybe_restore_boost_temp(now)
        if new_tm.get("is_active") and (not old_tm.get("is_active") or new_preset != old_preset):
            self._on_boost_start(now, new_tm, old_preset, startup, self._change_origin(new))
            return
        if old_tm.get("is_active") and not new_tm.get("is_active") and new_preset in (old_tm.get("original_preset"), old_preset):
            self._on_boost_end(now)
            return
        if new_tm.get("is_active"):
            return  # VTherm settling inside a running boost (its target moves with the boost preset)
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
            change = {"kind": kind, "from": old_preset, "to": new_preset, "magnitude": magnitude, "at": now.isoformat()}
            if kind == "setpoint":
                change.update({"from_target": old_target, "to_target": new_target})
            self._record_external_change(now, kind, old_preset, new_preset, magnitude, change, self._change_origin(new), startup=startup)
            return
        if old_target is not None and new_target is not None and abs(new_target - old_target) > 0.01:
            # Same preset, target moved: a preset-temp number edit (ours, or someone's) or a manual setpoint.
            if new.context.user_id is None and self._is_vtherm_reapply(new_preset, new_target, now, window):
                # D58: the VTherm putting its own preset temperature back (window closed, options reload, our preset
                # write landing) is not a person's or an automation's change. A person dragging the target back onto
                # the preset's own temperature carries their context and is theirs (D67).
                self._ignore_override(now, "temp", "vtherm_reapply")
                return
            number_entity = None
            for preset in (new_preset,) if new_preset else ():
                number_entity = self.vtherm.preset_number_entity(preset)
                if number_entity and self.write_log.made_by_us(number_entity, new_target, now, window):
                    return
            magnitude = new_target - old_target
            change = {
                "kind": "temp",
                "from": old_target,
                "to": new_target,
                "from_target": old_target,
                "to_target": new_target,
                "magnitude": magnitude,
                "at": now.isoformat(),
            }
            self._record_external_change(
                now, "temp", new_preset, new_preset, magnitude, change, self._change_origin(new, number_entity), startup=startup
            )

    def _is_vtherm_reapply(self, preset: str | None, target: float, now: datetime, window: timedelta) -> bool:
        """True when the new target is exactly the preset's own number and nobody touched that number recently.

        A manual setpoint switches a VTherm to preset none, and a preset-number edit (ours or a person's) moves the
        number within the match window, so neither is caught here.
        """
        if not preset or preset == PRESET_NONE:
            return False
        value = self.vtherm.preset_temp(preset)
        if value is None or abs(target - value) > 0.05:
            return False
        number_entity = self.vtherm.preset_number_entity(preset)
        state = self.hass.states.get(number_entity) if number_entity else None
        return state is None or now - state.last_changed > window

    @staticmethod
    def _not_ready(state) -> bool:
        return state is None or state.state in ("unavailable", "unknown") or state.attributes.get("is_ready") is False

    def _is_startup_change(self, old, now: datetime) -> bool:
        """D55: a change out of a VTherm that was unavailable or still initialising is its restore, not an override;
        so is any change in the grace window after Home Assistant finished starting, or (D66) within `reload_grace_s`
        of the VTherm coming back."""
        if self._not_ready(old):
            return True
        until = getattr(self, "_startup_grace_until", None)
        if until is not None and now <= until:
            return True
        return self._reload_grace_until is not None and now <= self._reload_grace_until

    def _record_external_change(
        self,
        now: datetime,
        kind: str,
        old_preset: str | None,
        new_preset: str | None,
        magnitude: float | None,
        change: dict[str, Any],
        origin: str,
        *,
        startup: bool = False,
    ) -> None:
        """A change Hearth did not make: a VTherm restoring itself at startup (D55), the Scheduler's (D48), or
        somebody's. Somebody's is judged by `origin` (D47)."""
        if startup and origin != ORIGIN_HUMAN:
            self._ignore_override(now, kind, "startup")
            self.request_reapply()
            return
        if self.scheduler_grace_active(now):
            self._ignore_override(now, kind, "scheduler_source")
            self.request_reapply()
            return
        if origin == ORIGIN_HUMAN and self._fold_stepped_change(now, change):
            return
        self._capture_override_undo(now)
        self.last_external_change_at = now
        self.last_external_change = {**change, "origin": origin}
        self._coalesce = None  # a change that was not folded starts afresh
        if origin == ORIGIN_HUMAN and _f(change.get("from_target")) is not None:
            self._coalesce = {
                "kind": kind,
                "from": _f(change["from_target"]),
                "to": _f(change.get("to_target")),
                "first_at": now,
                "last_at": now,
                "steps": 1,
                "old_preset": old_preset,
                "entry_at": None,  # set once the first step has made its ledger entry
                "ctx": None,
                "template": None,
            }
            self._arm_coalesce_timer()
        self._handle_override(now, kind, old_preset, new_preset, magnitude, origin)

    # ------------------------------------------------------------- stepped changes (D67)

    def _fold_stepped_change(self, now: datetime, change: dict[str, Any]) -> bool:
        """A person's target change arriving in steps (a slider drag, repeated taps) is one override.

        The Office on 26 Oct 09:22: 21 -> 19 in 0.1 C steps over six seconds. The first step was recorded as a -0.1 C
        baseline_error, the other twenty were "storm" and each restarted the stand-down (21 log lines, 21 state writes).
        Within `override_coalesce_s` of the previous step a further temp change is folded into the pending override:
        the ledger entry (if the first step made one) is amended to the net change from the first step's starting
        target, the stand-down is left alone, and one summary line is logged once the steps stop.
        """
        pending = self._coalesce
        if pending is None or now - pending["last_at"] > timedelta(seconds=float(self.const("override_coalesce_s"))):
            return False
        to = _f(change.get("to_target"))
        if to is None:
            return False  # not a target change (a preset tap): judged on its own
        pending.update({"last_at": now, "to": to, "steps": pending["steps"] + 1})
        net = to - pending["from"]
        self.last_external_change_at = now
        self.last_external_change = {
            "kind": pending["kind"],
            "from": pending["from"],
            "to": to,
            "magnitude": net,
            "at": now.isoformat(),
            "origin": ORIGIN_HUMAN,
            "steps": pending["steps"],
        }
        self._amend_coalesced_entry(pending, net)
        self._arm_coalesce_timer()
        return True

    def _amend_coalesced_entry(self, pending: dict[str, Any], net: float) -> None:
        """Rewrite the ledger entry the first step made so it carries the whole change.

        A drag back to where it started removes the entry (nothing was said); a drag that then carries on past the
        start puts one back. An entry something else made since (a boost) is left alone.
        """
        ctx, template, entry_at = pending.get("ctx"), pending.get("template"), pending.get("entry_at")
        if ctx is None or template is None:
            return  # the first step made no entry (settling, a storm...), so there is nothing to amend
        present = entry_at is not None and bool(self.ledger.entries) and self.ledger.entries[-1].at == entry_at
        if abs(net) < 0.01:
            if present:
                self.ledger.amend_last(None, self.half_life)
                self.learning.pop("last_override", None)
                pending["entry_at"] = None
            return
        direction = 1 if net > 0 else -1
        bucket, weight = attribute(
            ctx,
            direction,
            preheat_window_min=float(self.const("preheat_shortfall_window_min")),
            skip_after_window_min=float(self.const("skip_ended_learning_window_min")),
        )
        capped = min(abs(net), 2 * float(self.const("learning_target_bound")))
        entry = replace(template, direction=direction, magnitude=capped, bucket=bucket, weight=weight)
        if present:
            self.ledger.amend_last(entry, self.half_life)
        elif entry_at is None:
            self.ledger.add(entry, self.half_life)  # the bucket is already expressed as of entry.at: no decay
            pending["entry_at"] = entry.at
        else:
            return
        self.learning["last_override"] = {**entry.to_dict(), "raw_magnitude": net, "steps": pending["steps"]}

    def _arm_coalesce_timer(self) -> None:
        self._cancel_coalesce_timer()
        self._coalesce_timer = async_call_later(self.hass, float(self.const("override_coalesce_s")), self._on_coalesce_settled)

    def _cancel_coalesce_timer(self) -> None:
        if self._coalesce_timer is not None:
            self._coalesce_timer()
            self._coalesce_timer = None

    @callback
    def _on_coalesce_settled(self, _now) -> None:
        self._coalesce_timer = None
        pending, self._coalesce = self._coalesce, None
        if pending is None or pending["steps"] < 2:
            return
        net = (pending["to"] - pending["from"]) if pending.get("to") is not None else 0.0
        seconds = (pending["last_at"] - pending["first_at"]).total_seconds()
        last = self.learning.get("last_override") if pending.get("entry_at") is not None else None
        said = f" -> {last['bucket']}" if last else " (not learnt)"
        self._diagnostic(
            "override",
            f"Override settled: {pending['kind']} {'+' if net > 0 else '-'}{abs(net):.1f} C{said} "
            f"({pending['steps']} steps over {seconds:.0f} s)",
        )
        self._schedule_save()

    def _capture_override_undo(self, now: datetime) -> None:
        """Remember what an external change is about to alter, so a Scheduler slot that turns out to have caused it
        (its source switch event arrives after its write, D48) can be taken back."""
        self._override_undo = {
            "at": now,
            "standdown": self.standdown.to_dict(),
            "ledger": self.ledger.to_dict(),
            "last_override": copy.deepcopy(self.learning.get("last_override")),
            "ignored": len(self.learning.get("ignored", [])),
            "external_at": self.last_external_change_at,
            "external": self.last_external_change,
        }

    def undo_recent_override(self, now: datetime) -> bool:
        """Take back the stand-down and ledger entry of an external change made within the grace window."""
        undo = self._override_undo
        self._override_undo = None
        grace = timedelta(seconds=float(self.const("source_reenable_grace_s")))
        if undo is None or now - undo["at"] > grace:
            return False
        self.standdown = StandDown.from_dict(undo["standdown"])
        self.ledger = Ledger.from_dict(undo["ledger"])
        if undo["last_override"] is None:
            self.learning.pop("last_override", None)
        else:
            self.learning["last_override"] = undo["last_override"]
        del self.learning.setdefault("ignored", [])[undo["ignored"] :]
        kind = (self.last_external_change or {}).get("kind", "preset")
        self.last_external_change_at = undo["external_at"]
        self.last_external_change = undo["external"]
        self._ignore_override(now, kind, "scheduler_source")
        return True

    def _handle_override(
        self,
        now: datetime,
        kind: str,
        old_preset: str | None,
        new_preset: str | None,
        magnitude: float | None,
        origin: str = ORIGIN_HUMAN,
    ) -> None:
        if not self.enabled(SWITCH_LEARNING):
            return
        if self.dormant:
            self._ignore_override(now, kind, "dormant")
            return
        affected = tuple(self.affected_presets)
        if old_preset not in affected and not (kind == "preset" and new_preset in affected):
            self._ignore_override(now, kind, "unaffected_preset")
            return
        self._start_standdown(now, f"{'manual' if origin == ORIGIN_HUMAN else 'automation'}_{kind}")
        if origin != ORIGIN_HUMAN:
            # D47: Hearth stays out of the automation's way but does not learn from it
            self._ignore_override(now, kind, "automation")
            return
        self._record_override(now, kind, old_preset, new_preset, magnitude)

    def _record_override(self, now: datetime, kind: str, old_preset: str | None, new_preset: str | None, magnitude: float | None) -> None:
        if magnitude is None or abs(magnitude) < 0.01:
            self._ignore_override(now, kind, "no_magnitude")
            return
        if settling := self._settling(now):
            self._ignore_override(now, kind, settling)
            return
        direction = 1 if magnitude > 0 else -1
        ctx = self._override_context(now, old_preset)
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
        self._add_ledger_entry(now, ctx, direction, magnitude, bucket, weight, kind, old_preset, raw_magnitude=magnitude)
        if self._coalesce is not None and self._coalesce["first_at"] == now:
            # D67: later steps of the same change amend this entry
            self._coalesce.update({"entry_at": now, "ctx": ctx, "template": self.ledger.entries[-1]})

    def _override_context(self, now: datetime, active_preset: str | None) -> OverrideContext:
        last_q = self.ledger.last_entry_at()
        schedule_on = self.enabled("schedule") and not self.schedule_model.is_empty
        recent_wb = self.schedule_model.recent_warm_by(now, self.tz, timedelta(hours=6)) if schedule_on else None
        return OverrideContext(
            dormant=self.dormant,
            forecast_stale=not self.forecast_fresh(now),
            active_preset=active_preset,
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

    def _add_ledger_entry(
        self,
        now: datetime,
        ctx: OverrideContext,
        direction: int,
        magnitude: float,
        bucket: str,
        weight: float,
        kind: str,
        old_preset: str | None,
        *,
        raw_magnitude: float | None = None,
    ) -> None:
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
        self.learning["last_override"] = {**entry.to_dict(), "raw_magnitude": raw_magnitude}
        self._diagnostic("override", f"Override recorded: {kind} {'+' if direction > 0 else '-'}{capped:.1f} C -> {bucket}")

    # ------------------------------------------------------------- boosts (D60)

    def _on_boost_start(self, now: datetime, timed: dict[str, Any], old_preset: str | None, startup: bool, origin: str) -> None:
        """A VTherm timed preset started. Hearth stands down for exactly the boost, then learns what it says."""
        end = dt_util.parse_datetime(str(timed.get("end_time"))) if timed.get("end_time") else None
        if end is not None:
            minutes = max(1.0, (dt_util.as_utc(end) - now).total_seconds() / 60)
        else:
            minutes = float(timed.get("remaining_time_min") or self.const("boost_default_min"))
        self._start_standdown(now, STANDDOWN_TIMED_PRESET, minutes)
        pending = self._take_pending_boost(now)
        wanted = None
        if pending is not None:
            # D61: hearth.boost says who asked and for what temperature; the VTherm write itself carries no user
            origin = ORIGIN_HUMAN if pending["source"] in BOOST_SOURCES_PERSON else ORIGIN_AUTOMATION
            wanted = pending.get("temperature")
            if pending.get("restore") is not None:
                until = now + timedelta(minutes=minutes)
                self.learning["active_boost"] = {"restore": pending["restore"], "until": until.isoformat()}
        self.last_external_change_at = now  # a boost taints a heating run in progress
        self.last_external_change = {
            "kind": "boost",
            "to": timed.get("preset"),
            "at": now.isoformat(),
            "origin": origin,
            "source": pending["source"] if pending else None,
            "temperature": wanted,
        }
        if startup:
            self._ignore_override(now, "boost", "startup")
            return
        if origin != ORIGIN_HUMAN:
            self._ignore_override(now, "boost", "automation")
            return
        if not self.enabled(SWITCH_LEARNING):
            return
        if self.dormant:
            self._ignore_override(now, "boost", "dormant")
            return
        self._record_boost(now, timed.get("original_preset") or old_preset, wanted)

    def _on_boost_end(self, now: datetime) -> None:
        """The VTherm put the original preset back (or the boost was cancelled): not an override."""
        self._ignore_override(now, "boost", "boost_ended")
        self._mark_settle("boost_end", now)
        self.sched["boost_ended_at"] = now.isoformat()  # D63: a warm-by block close by takes over from the boost
        self._maybe_restore_boost_temp(now, force=True)
        if self.standdown.cause == STANDDOWN_TIMED_PRESET:
            self.standdown.clear()
        self.request_reapply()

    def _record_boost(self, now: datetime, original: str | None, wanted: float | None = None) -> None:
        """What a boost says depends on when it happened (D60) and, when a temperature was asked for, how far (D61)."""
        if settling := self._settling(now):
            self._ignore_override(now, "boost", settling)
            return
        affected = tuple(self.affected_presets)
        target = self.vtherm.preset_temp(original) if original else None
        direction, magnitude, raw = 1, float(self.const("boost_signal_c")), None
        if wanted is not None and target is not None:
            raw = wanted - target
            if abs(raw) < 0.25:
                self._ignore_override(now, "boost", "no_magnitude")
                return
            direction, magnitude = (1 if raw > 0 else -1), abs(raw)
        skip_window = float(self.const("skip_ended_learning_window_min"))
        skip_recent = direction > 0 and (
            self.skip.get("status") == "active" or (self.skip_ended_minutes_ago is not None and self.skip_ended_minutes_ago <= skip_window)
        )
        if original not in affected and not skip_recent:
            if direction > 0:
                self._record_boost_pattern(now, original)
            else:
                self._ignore_override(now, "boost", "unaffected_preset")  # turning a low room down says nothing
            return
        ctx = self._override_context(now, original if original in affected else (affected[0] if affected else original))
        exclusion = qualifies(ctx)
        if exclusion:
            self._ignore_override(now, "boost", exclusion)
            return
        current = self.snapshot.current_temp
        margin = float(self.const("boost_below_target_margin"))
        below = direction > 0 and target is not None and current is not None and current < target - margin
        warm_by_passed = ctx.minutes_since_warm_by is not None and ctx.minutes_since_warm_by >= 0
        if skip_recent:
            bucket, weight = BUCKET_SKIP, 2.0
        elif below and self.preheat_latched:
            # Still before the warm-by time: the warm-up is not late yet, so it says nothing about preheat (D64)
            self._ignore_override(now, "boost", "before_warm_by")
            return
        elif below and warm_by_passed:
            bucket, weight = BUCKET_PREHEAT, 1.0  # still warming up after warm-by: the warm-up was late, comfort may be fine
        else:
            bucket, weight = attribute(
                ctx,
                direction,
                preheat_window_min=float(self.const("preheat_shortfall_window_min")),
                skip_after_window_min=skip_window,
            )
        # Without a chosen temperature a boost's size is the boost preset's, not the person's: direction only, at a
        # fixed small magnitude. With one (a knob turn, D61) the difference from the target is the person's own.
        self._add_ledger_entry(now, ctx, direction, magnitude, bucket, weight, "boost", original, raw_magnitude=raw)

    def _record_boost_pattern(self, now: datetime, original: str | None) -> None:
        local = dt_util.as_local(now)
        non_workday = local.weekday() >= 5 or local.date() in self.schedule_model.non_workdays
        window = float(self.const("boost_pattern_window_days"))
        events = [BoostEvent.from_dict(e) for e in self.learning.get("boost_events", [])]
        events.append(BoostEvent(now, NON_WORKDAY if non_workday else WORKDAY, local.hour, original))
        events = prune(events, now, window)[-100:]
        self.learning["boost_events"] = [e.to_dict() for e in events]
        before = {(s["day_type"], s["hour"]) for s in self.learning.get("boost_suggestions", [])}
        found = suggestions(events, now, window, int(self.const("boost_pattern_min_events")))
        self.learning["boost_suggestions"] = found
        for s in found:
            if (s["day_type"], s["hour"]) not in before:
                day = "workdays" if s["day_type"] == WORKDAY else "non-workdays"
                presets = ", ".join(s["presets"]) or "a low preset"
                self._diagnostic(
                    "schedule_hint",
                    f"{s['count']} boosts on {day} between {s['slot']} in the last {window:.0f} days while the schedule had "
                    f"the room on {presets}: consider a comfort block",
                )
        self._ignore_override(now, "boost", "schedule_pattern")

    async def async_service_boost(self, minutes: float, temperature: float | None, source: str) -> None:
        """D61: run a VTherm timed boost on Hearth's behalf, optionally at a chosen temperature, and say who asked."""
        now = dt_util.utcnow()
        restore = None
        if temperature is not None:
            lo, hi = float(self.const("boost_min_c")), float(self.const("boost_max_c"))
            value = min(max(round(float(temperature) * 2) / 2, lo), hi)
            previous = self.vtherm.preset_temp(PRESET_BOOST)
            if await self.async_write_preset_temp(PRESET_BOOST, value, lo, hi, now, "boost"):
                restore, temperature = previous, value
            else:
                self._diagnostic("boost", "No boost preset number on this VTherm: boosting at its usual boost temperature")
                temperature = None
        self._pending_boost = {"at": now, "source": source, "temperature": temperature, "restore": restore}
        try:
            await self.hass.services.async_call(
                "versatile_thermostat",
                "set_timed_preset",
                {"entity_id": self.vtherm.entity_id, "preset": PRESET_BOOST, "duration_minutes": max(1, round(minutes))},
                blocking=True,
            )
        except Exception:
            self._pending_boost = None
            if restore is not None:
                await self._write_boost_temp(restore, now, "boost_restore")
            raise
        self._diagnostic(
            "boost",
            f"Boost for {minutes:.0f} min ({source}{f', {temperature:.1f} C' if temperature is not None else ''})",
        )

    def _take_pending_boost(self, now: datetime) -> dict[str, Any] | None:
        pending = getattr(self, "_pending_boost", None)
        self._pending_boost = None
        if pending is None or (now - pending["at"]).total_seconds() > float(self.const("boost_pending_s")):
            return None
        return pending

    def _maybe_restore_boost_temp(self, now: datetime, *, force: bool = False) -> None:
        """Put the boost preset temperature back after a hearth.boost at a chosen temperature (also after a restart)."""
        active = self.learning.get("active_boost")
        if not active:
            return
        until = datetime.fromisoformat(active["until"]) if active.get("until") else None
        if not force and until is not None and now < until:
            return
        self.learning.pop("active_boost", None)
        if active.get("restore") is not None:
            self.hass.async_create_task(self._write_boost_temp(float(active["restore"]), now, "boost_restore"))

    async def _write_boost_temp(self, value: float, now: datetime, reason: str) -> None:
        lo, hi = float(self.const("boost_min_c")), float(self.const("boost_max_c"))
        await self.async_write_preset_temp(PRESET_BOOST, min(max(value, lo), hi), lo, hi, now, reason)

    def boost_suggestions(self, now: datetime) -> list[dict[str, Any]]:
        events = [BoostEvent.from_dict(e) for e in self.learning.get("boost_events", [])]
        return suggestions(events, now, float(self.const("boost_pattern_window_days")), int(self.const("boost_pattern_min_events")))

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
        if not self.wants(SWITCH_APPLY_LEARNING):
            return 0.0
        total = self._correction(BUCKET_BASELINE)
        t_rm = self.running_mean.t_rm
        if t_rm is not None and t_rm < float(self.const("t_ref")) - float(self.const("slope_error_cold_margin")):
            total += self._correction(BUCKET_SLOPE)
        bound = float(self.const("learning_target_bound"))
        return max(-bound, min(bound, total))

    def learned_skip_threshold_offset(self) -> float:
        if not self.wants(SWITCH_APPLY_LEARNING):
            return 0.0
        return self._correction(BUCKET_SKIP)

    def learned_safety_factor_multiplier(self) -> float:
        if not self.wants(SWITCH_APPLY_LEARNING):
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
            **self.last_reset_attrs(),
            "half_life_days": self.const("learning_half_life_days"),
            "min_events": self.const("learning_min_events"),
        }

    # ------------------------------------------------------------- services

    async def async_service_override(self, minutes: float | None) -> None:
        now = dt_util.utcnow()
        if self.observing:
            self._diagnostic("override_service", "Observe only: no stand-down started, Hearth is not controlling this room")
            return
        self._start_standdown(now, "service", minutes)
        if self.skip_active:
            await self._end_skip(now, "override_service", restore=True)
        self._schedule_save()
        await self.async_evaluate("override_service")

    async def async_service_reset_learning(self, bucket: str | None) -> None:
        valid = (*BUCKETS, RESET_WARMING_RATE)
        if bucket is not None and bucket not in valid:
            raise ServiceValidationError(f"Unknown bucket {bucket!r}; expected one of {', '.join(valid)}")
        # D41: keep what is about to be cleared so hearth.undo_reset can put it back
        await self._stash_reset(bucket)
        if bucket == RESET_WARMING_RATE:
            # D38: the warming-rate model is not a ledger bucket. Clear it, drop any run in progress
            # (it was measured against the same sensor) and the cached preheat plan.
            runs = self.warming_model.n_runs
            self.warming_model.reset()
            self.heating_run = None
            self.preheat_plan = None
            self._diagnostic("reset_learning", f"Warming-rate model reset ({runs} runs discarded)")
        else:
            self.ledger.reset(bucket)
            self._diagnostic("reset_learning", f"Ledger reset ({bucket or 'all buckets'})")
        await self._async_commit_learning("reset_learning")


def _f(value) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
