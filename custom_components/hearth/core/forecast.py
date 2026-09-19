"""Forecast cache model and the decisions that read it (spec 4.2, 4.3, 4.7).

Decisions never fetch. They only read a normalised `ForecastCache`.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

SUNNY_CONDITIONS = frozenset({"sunny", "partlycloudy", "clear-night", "windy"})


@dataclass(frozen=True)
class HourlyPoint:
    """One hourly forecast point (aware datetime)."""

    at: datetime
    temperature: float
    condition: str | None = None

    def to_dict(self) -> dict:
        return {"at": self.at.isoformat(), "temperature": self.temperature, "condition": self.condition}

    @classmethod
    def from_dict(cls, data: dict) -> HourlyPoint:
        return cls(datetime.fromisoformat(data["at"]), float(data["temperature"]), data.get("condition"))


@dataclass(frozen=True)
class DailyPoint:
    """One daily forecast point."""

    day: date
    high: float | None
    low: float | None = None
    condition: str | None = None

    def to_dict(self) -> dict:
        return {"day": self.day.isoformat(), "high": self.high, "low": self.low, "condition": self.condition}

    @classmethod
    def from_dict(cls, data: dict) -> DailyPoint:
        return cls(date.fromisoformat(data["day"]), data.get("high"), data.get("low"), data.get("condition"))


@dataclass
class ForecastCache:
    """Normalised forecast for one weather entity."""

    fetched_at: datetime | None = None
    hourly: list[HourlyPoint] = field(default_factory=list)
    daily: list[DailyPoint] = field(default_factory=list)
    source: str | None = None

    def to_dict(self) -> dict:
        return {
            "fetched_at": self.fetched_at.isoformat() if self.fetched_at else None,
            "hourly": [p.to_dict() for p in self.hourly],
            "daily": [p.to_dict() for p in self.daily],
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> ForecastCache:
        if not data:
            return cls()
        return cls(
            fetched_at=datetime.fromisoformat(data["fetched_at"]) if data.get("fetched_at") else None,
            hourly=[HourlyPoint.from_dict(p) for p in data.get("hourly", [])],
            daily=[DailyPoint.from_dict(p) for p in data.get("daily", [])],
            source=data.get("source"),
        )

    def is_fresh(self, now: datetime, max_age: timedelta) -> bool:
        """Cache is usable when it was fetched within `max_age` of `now`."""
        if self.fetched_at is None:
            return False
        age = now - self.fetched_at
        return timedelta(0) <= age <= max_age

    def hourly_between(self, start: datetime, end: datetime) -> list[HourlyPoint]:
        """Hourly points with start <= at < end."""
        return [p for p in self.hourly if start <= p.at < end]

    def temperature_at(self, when: datetime) -> float | None:
        """Nearest hourly temperature within 90 minutes of `when`, else None."""
        best: HourlyPoint | None = None
        best_gap = timedelta(minutes=90)
        for p in self.hourly:
            gap = abs(p.at - when)
            if gap < best_gap:
                best, best_gap = p, gap
        return best.temperature if best else None

    def daily_for(self, day: date) -> DailyPoint | None:
        for p in self.daily:
            if p.day == day:
                return p
        return None

    def high_for_day(self, day: date, day_start: datetime, day_end: datetime, cutoff: datetime | None = None) -> float | None:
        """Forecast high for `day`.

        With `cutoff` (hourly mode) use the max of the hourly series between
        `day_start` and `cutoff`. Otherwise prefer the daily point's high, falling
        back to the max of the hourly series for the whole day.
        """
        if cutoff is not None:
            pts = self.hourly_between(day_start, cutoff)
            return max((p.temperature for p in pts), default=None)
        daily = self.daily_for(day)
        if daily is not None and daily.high is not None:
            return daily.high
        pts = self.hourly_between(day_start, day_end)
        return max((p.temperature for p in pts), default=None)

    def condition_for_day(self, day: date, morning_start: datetime, morning_end: datetime) -> str | None:
        """Daily condition, else the most common hourly condition in the morning window."""
        daily = self.daily_for(day)
        if daily is not None and daily.condition:
            return daily.condition
        conds = [p.condition for p in self.hourly_between(morning_start, morning_end) if p.condition]
        if not conds:
            return None
        return Counter(conds).most_common(1)[0][0]

    def coldest_between(self, start: datetime, end: datetime) -> float | None:
        pts = self.hourly_between(start, end)
        return min((p.temperature for p in pts), default=None)


def skip_threshold(
    base_threshold: float,
    condition: str | None,
    solar_room: bool,
    solar_boost: float,
    sunny_conditions: frozenset[str] = SUNNY_CONDITIONS,
) -> float:
    """skip_threshold = base_threshold - solar_discount(condition) (spec 4.2)."""
    if solar_room and condition in sunny_conditions:
        return base_threshold - solar_boost
    return base_threshold


def derived_base_threshold(base_threshold: float, t_rm: float | None, t_ref: float, factor: float) -> float:
    """Optional post-warm-spell lowering of the base threshold from T_rm (Tier 3 flag).

    threshold = base - factor * max(0, T_rm - T_ref). Only ever lowers it.
    """
    if t_rm is None:
        return base_threshold
    return base_threshold - factor * max(0.0, t_rm - t_ref)


@dataclass(frozen=True)
class SkipDecision:
    """Outcome of the forecast-skip test."""

    skip: bool
    reason: str
    threshold: float | None = None
    forecast_high: float | None = None
    condition: str | None = None


def decide_skip(
    *,
    forecast_fresh: bool,
    forecast_high: float | None,
    condition: str | None,
    indoor_temp: float | None,
    min_indoor_floor: float,
    base_threshold: float,
    solar_room: bool,
    solar_boost: float,
    in_scope: bool = True,
) -> SkipDecision:
    """Decide whether to skip heating this morning (spec 4.2).

    Fails toward heating normally: any missing input means no skip.
    """
    if not in_scope:
        return SkipDecision(False, "out_of_scope")
    if not forecast_fresh:
        return SkipDecision(False, "forecast_stale")
    if forecast_high is None:
        return SkipDecision(False, "no_forecast_high")
    if indoor_temp is None:
        return SkipDecision(False, "no_indoor_temp")
    if indoor_temp < min_indoor_floor:
        return SkipDecision(False, "below_indoor_floor", forecast_high=forecast_high, condition=condition)
    threshold = skip_threshold(base_threshold, condition, solar_room, solar_boost)
    if forecast_high >= threshold:
        return SkipDecision(True, "forecast_warm", threshold, forecast_high, condition)
    return SkipDecision(False, "forecast_cold", threshold, forecast_high, condition)


def skip_abort_reason(
    *,
    indoor_temp: float | None,
    min_indoor_floor: float,
    recheck_due: bool,
    indoor_at_start: float | None,
    outdoor_actual: float | None,
    outdoor_forecast: float | None,
    outdoor_shortfall: float,
    manual_intervention: bool,
    dormant: bool,
) -> str | None:
    """Return an abort reason for an active skip, or None to keep skipping (spec 4.2)."""
    if indoor_temp is not None and indoor_temp < min_indoor_floor:
        return "below_indoor_floor"
    if manual_intervention:
        return "manual_intervention"
    if dormant:
        return "dormant"
    if recheck_due and indoor_temp is not None and indoor_at_start is not None:
        falling = indoor_temp < indoor_at_start
        if falling and outdoor_actual is not None and outdoor_forecast is not None:
            if outdoor_actual <= outdoor_forecast - outdoor_shortfall:
                return "forecast_shortfall"
    return None


@dataclass(frozen=True)
class SetbackDecision:
    """Outcome of the bedtime setback-depth test."""

    reduce: bool
    reason: str
    coldest: float | None = None
    eco_target: float | None = None


def decide_setback(
    *,
    forecast_fresh: bool,
    coldest_morning: float | None,
    cold_morning_threshold: float,
    eco_base: float | None,
    setback_reduction: float,
    ceiling: float,
) -> SetbackDecision:
    """Decide whether to raise the eco preset tonight (spec 4.3).

    `ceiling` is the hard upper bound for the eco write (band_max, and never
    above the comfort target when the caller passes that in).
    """
    if not forecast_fresh:
        return SetbackDecision(False, "forecast_stale")
    if coldest_morning is None:
        return SetbackDecision(False, "no_morning_forecast")
    if eco_base is None:
        return SetbackDecision(False, "no_eco_base")
    if coldest_morning >= cold_morning_threshold:
        return SetbackDecision(False, "morning_mild", coldest_morning)
    target = min(eco_base + setback_reduction, ceiling)
    if target <= eco_base:
        return SetbackDecision(False, "no_headroom", coldest_morning, eco_base)
    return SetbackDecision(True, "cold_morning", coldest_morning, target)
