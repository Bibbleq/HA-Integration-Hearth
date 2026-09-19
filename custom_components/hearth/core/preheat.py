"""Warming-rate model and preheat lead-time maths (spec 4.4)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta


@dataclass
class WarmingModel:
    """rate(T_out) = a - b * (T_target - T_out), fitted from clean heating runs.

    `runs` holds (delta, rate) pairs where delta = T_target - T_out (C) and
    rate is the observed warming rate (C/h). Bounded ring.
    """

    a: float | None = None
    b: float | None = None
    runs: list[tuple[float, float]] = field(default_factory=list)
    max_runs: int = 30

    @property
    def n_runs(self) -> int:
        return len(self.runs)

    def to_dict(self) -> dict:
        return {"a": self.a, "b": self.b, "runs": [list(r) for r in self.runs], "max_runs": self.max_runs}

    @classmethod
    def from_dict(cls, data: dict | None) -> WarmingModel:
        if not data:
            return cls()
        return cls(
            a=data.get("a"),
            b=data.get("b"),
            runs=[(float(r[0]), float(r[1])) for r in data.get("runs", [])],
            max_runs=int(data.get("max_runs", 30)),
        )

    def add_run(self, delta: float, rate: float) -> None:
        self.runs.append((float(delta), float(rate)))
        del self.runs[: -self.max_runs]
        self.a, self.b = fit(self.runs)

    def reset(self) -> None:
        self.a = self.b = None
        self.runs.clear()


def fit(runs: list[tuple[float, float]]) -> tuple[float | None, float | None]:
    """Least-squares fit of rate = a - b * delta.

    Falls back to b = 0 (a = mean rate) when delta has no spread. Returns
    (None, None) with no runs. b is forced non-negative: a bigger deficit can
    never make the room warm faster.
    """
    n = len(runs)
    if n == 0:
        return None, None
    mean_x = sum(d for d, _ in runs) / n
    mean_y = sum(r for _, r in runs) / n
    sxx = sum((d - mean_x) ** 2 for d, _ in runs)
    if n < 2 or sxx < 1e-6:
        return mean_y, 0.0
    sxy = sum((d - mean_x) * (r - mean_y) for d, r in runs)
    slope = sxy / sxx  # rate per unit delta, expected negative
    b = max(0.0, -slope)
    a = mean_y + b * mean_x
    return a, b


def warming_rate(
    model: WarmingModel,
    t_target: float,
    t_out: float | None,
    *,
    min_runs: int = 5,
    default_rate: float = 1.0,
    rate_min: float = 0.3,
    rate_max: float = 3.0,
) -> tuple[float, bool]:
    """Warming rate in C/h at the given target and outdoor temperature.

    Returns (rate, learned). Until `min_runs` clean runs exist, or when the
    model or outdoor temperature is missing, the conservative default applies.
    """
    if model.a is None or model.n_runs < min_runs or t_out is None:
        return max(rate_min, min(rate_max, default_rate)), False
    b = model.b or 0.0
    rate = model.a - b * (t_target - t_out)
    return max(rate_min, min(rate_max, rate)), True


def lead_minutes(
    deficit: float,
    rate: float,
    *,
    safety_factor: float = 1.15,
    max_preheat_min: float = 120.0,
) -> float:
    """lead = deficit / rate * safety_factor, capped at max_preheat, never negative."""
    if deficit <= 0 or rate <= 0:
        return 0.0
    lead = deficit / rate * 60.0 * safety_factor
    return min(lead, max_preheat_min)


def preheat_start(warm_by: datetime, lead_min: float) -> datetime:
    """start = warm_by - lead. Computed on aware datetimes so DST is handled by the tz."""
    return warm_by - timedelta(minutes=lead_min)


@dataclass
class HeatingRun:
    """Samples collected during one heating run for the learner."""

    started_at: datetime
    t_target: float
    start_temp: float
    last_temp: float
    last_at: datetime
    outdoor_sum: float = 0.0
    outdoor_count: int = 0
    tainted: bool = False
    taint_reason: str | None = None

    def sample(self, at: datetime, temp: float, outdoor: float | None) -> None:
        self.last_temp = temp
        self.last_at = at
        if outdoor is not None:
            self.outdoor_sum += outdoor
            self.outdoor_count += 1

    def taint(self, reason: str) -> None:
        self.tainted = True
        self.taint_reason = reason

    @property
    def outdoor_mean(self) -> float | None:
        return self.outdoor_sum / self.outdoor_count if self.outdoor_count else None

    def result(self, *, min_minutes: float = 20.0, min_rise: float = 0.3) -> tuple[float, float] | None:
        """(delta, rate) for a clean run long enough to trust, else None."""
        if self.tainted or self.outdoor_mean is None:
            return None
        hours = (self.last_at - self.started_at).total_seconds() / 3600.0
        rise = self.last_temp - self.start_temp
        if hours * 60.0 < min_minutes or rise < min_rise:
            return None
        rate = rise / hours
        delta = self.t_target - self.outdoor_mean
        return delta, rate

    def to_dict(self) -> dict:
        return {
            "started_at": self.started_at.isoformat(),
            "t_target": self.t_target,
            "start_temp": self.start_temp,
            "last_temp": self.last_temp,
            "last_at": self.last_at.isoformat(),
            "outdoor_sum": self.outdoor_sum,
            "outdoor_count": self.outdoor_count,
            "tainted": self.tainted,
            "taint_reason": self.taint_reason,
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> HeatingRun | None:
        if not data:
            return None
        return cls(
            started_at=datetime.fromisoformat(data["started_at"]),
            t_target=float(data["t_target"]),
            start_temp=float(data["start_temp"]),
            last_temp=float(data["last_temp"]),
            last_at=datetime.fromisoformat(data["last_at"]),
            outdoor_sum=float(data.get("outdoor_sum", 0.0)),
            outdoor_count=int(data.get("outdoor_count", 0)),
            tainted=bool(data.get("tainted", False)),
            taint_reason=data.get("taint_reason"),
        )
