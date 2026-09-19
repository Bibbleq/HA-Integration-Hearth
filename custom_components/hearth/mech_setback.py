"""Setback depth (spec 4.3): shallower overnight eco before very cold mornings."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any

from homeassistant.util import dt as dt_util

from .const import (
    CONF_BAND_MAX,
    CONF_BEDTIME_DECISION_TIME,
    NUMBER_COLD_MORNING,
    PRESET_ECO,
    SWITCH_SETBACK,
    TIER2_DEFAULTS,
)
from .core.forecast import decide_setback
from .mech_skip import _dt, local_at

_LOGGER = logging.getLogger(__name__)


class SetbackMixin:
    """Bedtime decision and morning restore. Mixed into HearthRoom."""

    @property
    def setback(self) -> dict[str, Any]:
        return self.mechanisms.setdefault("setback", {"active": False})

    def bedtime_decision_time(self, day: date) -> datetime:
        return local_at(day, self.option(CONF_BEDTIME_DECISION_TIME), TIER2_DEFAULTS[CONF_BEDTIME_DECISION_TIME])

    def setback_restore_time(self, night_of: date) -> datetime:
        return local_at(night_of + timedelta(days=1), self.const("setback_restore_time"), "09:00")

    def eco_ceiling(self) -> float:
        """Eco may never exceed band_max or the current comfort target."""
        ceiling = float(self.option(CONF_BAND_MAX))
        if self.comfort is not None:
            ceiling = min(ceiling, self.comfort.quantised)
        return ceiling

    async def _evaluate_setback(self, now: datetime) -> None:
        today = dt_util.as_local(now).date()
        sb = self.setback
        if sb.get("active"):
            await self._supervise_setback(now)
        elif sb.get("decided_date") != today.isoformat() and now >= self.bedtime_decision_time(today):
            await self._decide_setback(now, today)

    async def _decide_setback(self, now: datetime, today: date) -> None:
        sb = self.setback
        bedtime = self.bedtime_decision_time(today)
        late_window = timedelta(minutes=float(self.const("setback_late_decision_window_min")))
        if now > bedtime + late_window:
            sb.update({"decided_date": today.isoformat(), "decided_at": now.isoformat(), "reason": "missed"})
            return
        if not self.enabled(SWITCH_SETBACK) or self.dormant or self.standdown_active:
            reason = "disabled" if not self.enabled(SWITCH_SETBACK) else ("dormant" if self.dormant else "standdown")
            sb.update({"decided_date": today.isoformat(), "decided_at": now.isoformat(), "reason": reason})
            return
        tomorrow = today + timedelta(days=1)
        cache = self.forecast_cache()
        coldest = cache.coldest_between(
            local_at(tomorrow, self.const("forecast_morning_start"), "06:00"),
            local_at(tomorrow, self.const("forecast_morning_end"), "09:00"),
        )
        eco_base = self.vtherm.preset_temp(PRESET_ECO)
        decision = decide_setback(
            forecast_fresh=self.forecast_fresh(now),
            coldest_morning=coldest,
            cold_morning_threshold=self.number(NUMBER_COLD_MORNING),
            eco_base=eco_base,
            setback_reduction=float(self.const("setback_reduction")),
            ceiling=self.eco_ceiling(),
        )
        if decision.reason == "forecast_stale":
            sb["pending_reason"] = "forecast_stale"  # retry within the late window
            return
        sb.update(
            {
                "decided_date": today.isoformat(),
                "decided_at": now.isoformat(),
                "reason": decision.reason,
                "coldest": decision.coldest,
                "pending_reason": None,
            }
        )
        if not decision.reduce:
            return
        lo = min(float(eco_base), self.eco_ceiling())
        ok = await self.async_write_preset_temp(PRESET_ECO, decision.eco_target, lo, self.eco_ceiling(), now, "setback")
        if ok:
            sb.update(
                {
                    "active": True,
                    "night_of": today.isoformat(),
                    "eco_base": eco_base,
                    "eco_target": decision.eco_target,
                    "applied_at": now.isoformat(),
                    "restored_at": None,
                    "restore_reason": None,
                }
            )
            self._diagnostic("setback_start", f"Cold morning ({decision.coldest} C): eco raised {eco_base} -> {decision.eco_target}")

    async def _supervise_setback(self, now: datetime) -> None:
        sb = self.setback
        night_of = date.fromisoformat(sb["night_of"])
        restore_at = self.setback_restore_time(night_of)
        if now >= restore_at:
            await self._restore_setback(now, "morning_recovery")
        elif not self.enabled(SWITCH_SETBACK):
            await self._restore_setback(now, "disabled")

    async def _restore_setback(self, now: datetime, reason: str) -> None:
        sb = self.setback
        current = self.vtherm.preset_temp(PRESET_ECO)
        eco_base = sb.get("eco_base")
        eco_target = sb.get("eco_target")
        if current is not None and eco_base is not None and eco_target is not None and abs(current - float(eco_target)) < 0.01:
            lo = min(float(eco_base), self.eco_ceiling())
            await self.async_write_preset_temp(
                PRESET_ECO, float(eco_base), lo, max(lo, float(eco_target)), now, f"setback_restore_{reason}"
            )
        else:
            reason = f"{reason}_left_as_is"  # someone changed eco since; leave theirs
        sb.update({"active": False, "restored_at": now.isoformat(), "restore_reason": reason})
        self._diagnostic("setback_end", f"Setback restored: {reason}")

    @property
    def setback_status(self) -> str:
        return "active" if self.setback.get("active") else "idle"

    @property
    def setback_restore_due_at(self) -> datetime | None:
        sb = self.setback
        if not sb.get("active"):
            return None
        return self.setback_restore_time(date.fromisoformat(sb["night_of"]))


__all__ = ["SetbackMixin", "_dt"]
