"""Adaptive comfort maths (spec 4.1).

Running mean of outdoor temperature (EN 16798 exponentially weighted form)
and the adaptive comfort target derived from it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
import math


def clamp(value: float, lo: float, hi: float) -> float:
    """Hard clamp `value` into [lo, hi]. lo > hi is treated as a degenerate band at lo."""
    if lo > hi:
        hi = lo
    return max(lo, min(hi, value))


def quantise(value: float, step: float = 0.5) -> float:
    """Round to the nearest multiple of `step` (half away from zero)."""
    if step <= 0:
        return value
    scaled = value / step
    rounded = math.floor(abs(scaled) + 0.5) * (1 if scaled >= 0 else -1)
    return round(rounded * step, 6)


def quantise_within(value: float, step: float, lo: float, hi: float) -> float:
    """Quantise `value` to `step` such that the result stays within [lo, hi].

    If the nearest step sits outside the band, step back inward. If the band is
    narrower than one step and contains no multiple of step, return the clamped
    (unquantised) value so the hard clamp always wins over the quantise.
    """
    clamped = clamp(value, lo, hi)
    q = quantise(clamped, step)
    if q > hi:
        q = round(math.floor(hi / step) * step, 6)
    if q < lo:
        q = round(math.ceil(lo / step) * step, 6)
    if q < lo or q > hi:
        return clamped
    return q


def update_running_mean(t_rm_prev: float, t_mean_yesterday: float, alpha: float = 0.8) -> float:
    """EN 16798 running mean: T_rm = (1 - alpha) * T_mean(yesterday) + alpha * T_rm(yesterday)."""
    return (1.0 - alpha) * t_mean_yesterday + alpha * t_rm_prev


def seed_running_mean(daily_means: list[float], alpha: float = 0.8, fallback: float | None = None) -> float | None:
    """Seed T_rm from a list of historical daily means, oldest first.

    Starts from the oldest mean and applies the EWMA forward. Returns
    `fallback` when there is no history.
    """
    if not daily_means:
        return fallback
    t_rm = daily_means[0]
    for mean in daily_means[1:]:
        t_rm = update_running_mean(t_rm, mean, alpha)
    return t_rm


@dataclass
class DayAccumulator:
    """Accumulates outdoor samples for one calendar day."""

    day: str  # ISO date
    total: float = 0.0
    count: int = 0

    def add(self, value: float) -> None:
        self.total += value
        self.count += 1

    @property
    def mean(self) -> float | None:
        return self.total / self.count if self.count else None

    def to_dict(self) -> dict:
        return {"day": self.day, "total": self.total, "count": self.count}

    @classmethod
    def from_dict(cls, data: dict | None) -> DayAccumulator | None:
        if not data:
            return None
        return cls(day=data["day"], total=float(data.get("total", 0.0)), count=int(data.get("count", 0)))


@dataclass
class RunningMeanState:
    """Persistent state of the running-mean tracker.

    `t_rm` is the running mean as of `t_rm_day` (the last completed day folded in).
    `current` accumulates today's samples; `completed` holds a finished day that
    has not yet been folded into `t_rm`.
    """

    t_rm: float | None = None
    t_rm_day: str | None = None
    seeded_at: str | None = None
    frozen: bool = False
    current: DayAccumulator | None = None
    completed: DayAccumulator | None = None
    history: list[float] = field(default_factory=list)  # recent daily means, newest last

    def to_dict(self) -> dict:
        return {
            "t_rm": self.t_rm,
            "t_rm_day": self.t_rm_day,
            "seeded_at": self.seeded_at,
            "frozen": self.frozen,
            "current": self.current.to_dict() if self.current else None,
            "completed": self.completed.to_dict() if self.completed else None,
            "history": list(self.history),
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> RunningMeanState:
        if not data:
            return cls()
        return cls(
            t_rm=data.get("t_rm"),
            t_rm_day=data.get("t_rm_day"),
            seeded_at=data.get("seeded_at"),
            frozen=bool(data.get("frozen", False)),
            current=DayAccumulator.from_dict(data.get("current")),
            completed=DayAccumulator.from_dict(data.get("completed")),
            history=[float(x) for x in data.get("history", [])],
        )


class RunningMeanTracker:
    """Drives RunningMeanState from samples and day rolls. Idempotent by design."""

    def __init__(self, state: RunningMeanState, alpha: float = 0.8, history_len: int = 14) -> None:
        self.state = state
        self.alpha = alpha
        self.history_len = history_len

    def seed(self, value: float, today: date, seeded_at_iso: str) -> None:
        """Seed T_rm if we have no value yet.

        The seed stands for the running mean as of the start of `today`, so the
        last folded day is recorded as yesterday and today's samples roll in at
        the next daily recompute.
        """
        if self.state.t_rm is None:
            self.state.t_rm = value
            self.state.t_rm_day = (today - timedelta(days=1)).isoformat()
            self.state.seeded_at = seeded_at_iso

    def add_sample(self, value: float, today: date) -> None:
        """Add an outdoor sample taken on `today` (local date)."""
        day = today.isoformat()
        cur = self.state.current
        if cur is None:
            self.state.current = DayAccumulator(day)
        elif cur.day != day:
            # Day changed: the old accumulator is complete. Only keep it if it
            # is newer than what has already been folded in.
            if cur.count and (self.state.t_rm_day is None or cur.day > self.state.t_rm_day):
                self.state.completed = cur
            self.state.current = DayAccumulator(day)
        self.state.current.add(value)
        self.state.frozen = False

    def roll(self, today: date) -> bool:
        """Fold the completed day into T_rm if it has not been folded already.

        Returns True when T_rm changed. Safe to call any number of times.
        """
        st = self.state
        # If the current accumulator belongs to a past day, promote it first.
        if st.current is not None and st.current.day < today.isoformat() and st.current.count:
            if st.t_rm_day is None or st.current.day > st.t_rm_day:
                st.completed = st.current
            st.current = None
        comp = st.completed
        if comp is None or comp.mean is None:
            return False
        if st.t_rm_day is not None and comp.day <= st.t_rm_day:
            st.completed = None
            return False
        mean = comp.mean
        if st.t_rm is None:
            st.t_rm = mean
        else:
            st.t_rm = update_running_mean(st.t_rm, mean, self.alpha)
        st.t_rm_day = comp.day
        st.history.append(round(mean, 3))
        del st.history[: -self.history_len]
        st.completed = None
        return True

    def freeze(self) -> None:
        """Outdoor sensor unavailable: hold T_rm at last value."""
        self.state.frozen = True


@dataclass(frozen=True)
class ComfortResult:
    """Result of an adaptive comfort computation."""

    raw_target: float  # T_base + slope * (T_rm - T_ref), unclamped
    offset_requested: float  # slope * (T_rm - T_ref)
    offset_applied: float  # after max_offset cap and band clamp
    target: float  # clamped, pre-quantise
    quantised: float  # the value to write
    held: bool = False  # offset held at zero (seed hold / frozen / no T_rm)


def comfort_target(
    t_base: float,
    slope: float,
    t_rm: float | None,
    t_ref: float,
    band_min: float,
    band_max: float,
    max_offset: float,
    step: float = 0.5,
    hold: bool = False,
    extra_offset: float = 0.0,
) -> ComfortResult:
    """Compute the adaptive comfort target (spec 4.1).

    T_comfort = clamp(T_base + slope * (T_rm - T_ref), band_min, band_max), with the
    total applied offset additionally capped at +/- max_offset and the result
    quantised to `step` within the band. `extra_offset` is a learned correction
    (phase 4); it is included in the offset cap and the band clamp, so no path
    can escape the clamps.
    """
    if hold or t_rm is None:
        requested = 0.0
    else:
        requested = slope * (t_rm - t_ref)
    total = requested + extra_offset
    capped = clamp(total, -abs(max_offset), abs(max_offset))
    raw = t_base + total
    target = clamp(t_base + capped, band_min, band_max)
    q = quantise_within(target, step, band_min, band_max)
    return ComfortResult(
        raw_target=raw,
        offset_requested=requested,
        offset_applied=target - t_base,
        target=target,
        quantised=q,
        held=hold or t_rm is None,
    )
