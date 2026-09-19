"""Override detection, stand-down and the learning ledger (spec 4.6)."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta

BUCKET_PREHEAT = "preheat_shortfall"
BUCKET_SLOPE = "slope_error"
BUCKET_SKIP = "skip_failure"
BUCKET_BASELINE = "baseline_error"
BUCKETS = (BUCKET_PREHEAT, BUCKET_SLOPE, BUCKET_SKIP, BUCKET_BASELINE)


# ---------------------------------------------------------------- write log


@dataclass(frozen=True)
class WriteRecord:
    """One write Hearth made to a VTherm entity."""

    entity_id: str
    value: str
    at: datetime
    reason: str = ""

    def to_dict(self) -> dict:
        return {"entity_id": self.entity_id, "value": self.value, "at": self.at.isoformat(), "reason": self.reason}

    @classmethod
    def from_dict(cls, data: dict) -> WriteRecord:
        return cls(data["entity_id"], str(data["value"]), datetime.fromisoformat(data["at"]), data.get("reason", ""))


class WriteLog:
    """Bounded ring of Hearth's own writes, used to tell our changes from manual ones."""

    def __init__(self, records: list[WriteRecord] | None = None, max_len: int = 100) -> None:
        self.records: list[WriteRecord] = list(records or [])
        self.max_len = max_len

    def record(self, entity_id: str, value, at: datetime, reason: str = "") -> WriteRecord:
        rec = WriteRecord(entity_id, _norm(value), at, reason)
        self.records.append(rec)
        del self.records[: -self.max_len]
        return rec

    def made_by_us(self, entity_id: str, value, at: datetime, window: timedelta) -> bool:
        """True if we wrote `value` to `entity_id` within `window` before `at`."""
        target = _norm(value)
        for rec in reversed(self.records):
            if rec.at > at:
                continue
            if at - rec.at > window:
                break
            if rec.entity_id == entity_id and rec.value == target:
                return True
        return False

    def last_for(self, entity_id: str) -> WriteRecord | None:
        for rec in reversed(self.records):
            if rec.entity_id == entity_id:
                return rec
        return None

    def to_list(self) -> list[dict]:
        return [r.to_dict() for r in self.records]

    @classmethod
    def from_list(cls, data: list | None, max_len: int = 100) -> WriteLog:
        return cls([WriteRecord.from_dict(d) for d in (data or [])], max_len)


def _norm(value) -> str:
    if isinstance(value, (int, float)):
        return f"{float(value):.2f}"
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return str(value)


# ---------------------------------------------------------------- stand-down


@dataclass
class StandDown:
    """Hearth stops acting on a room until `until`."""

    until: datetime | None = None
    cause: str | None = None
    started_at: datetime | None = None

    def active(self, now: datetime) -> bool:
        return self.until is not None and now < self.until

    def clear(self) -> None:
        self.until = self.cause = self.started_at = None

    def to_dict(self) -> dict:
        return {
            "until": self.until.isoformat() if self.until else None,
            "cause": self.cause,
            "started_at": self.started_at.isoformat() if self.started_at else None,
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> StandDown:
        if not data or not data.get("until"):
            return cls()
        return cls(
            until=datetime.fromisoformat(data["until"]),
            cause=data.get("cause"),
            started_at=datetime.fromisoformat(data["started_at"]) if data.get("started_at") else None,
        )


def standdown_until(now: datetime, next_boundary: datetime | None, timeout: timedelta) -> datetime:
    """Stand-down ends at the next block boundary or the timeout, whichever is sooner."""
    end = now + timeout
    if next_boundary is not None and now < next_boundary < end:
        return next_boundary
    return end


# ---------------------------------------------------------------- ledger


@dataclass(frozen=True)
class LedgerEntry:
    """One qualifying manual override."""

    at: datetime
    direction: int  # +1 warmer, -1 cooler
    magnitude: float  # C
    t_rm: float | None
    t_out: float | None
    minutes_since_preset_change: float | None
    skip_state: str
    applied_offset: float
    active_preset: str | None
    bucket: str
    weight: float = 1.0
    kind: str = "temp"  # temp | preset

    def to_dict(self) -> dict:
        return {
            "at": self.at.isoformat(),
            "direction": self.direction,
            "magnitude": self.magnitude,
            "t_rm": self.t_rm,
            "t_out": self.t_out,
            "minutes_since_preset_change": self.minutes_since_preset_change,
            "skip_state": self.skip_state,
            "applied_offset": self.applied_offset,
            "active_preset": self.active_preset,
            "bucket": self.bucket,
            "weight": self.weight,
            "kind": self.kind,
        }

    @classmethod
    def from_dict(cls, data: dict) -> LedgerEntry:
        return cls(
            at=datetime.fromisoformat(data["at"]),
            direction=int(data["direction"]),
            magnitude=float(data["magnitude"]),
            t_rm=data.get("t_rm"),
            t_out=data.get("t_out"),
            minutes_since_preset_change=data.get("minutes_since_preset_change"),
            skip_state=data.get("skip_state", "idle"),
            applied_offset=float(data.get("applied_offset", 0.0)),
            active_preset=data.get("active_preset"),
            bucket=data.get("bucket", BUCKET_BASELINE),
            weight=float(data.get("weight", 1.0)),
            kind=data.get("kind", "temp"),
        )


@dataclass(frozen=True)
class OverrideContext:
    """Everything attribution needs to know about the room at override time."""

    dormant: bool
    forecast_stale: bool
    active_preset: str | None
    affected_presets: tuple[str, ...]
    skip_state: str  # idle / preview / active / aborted
    skip_ended_minutes_ago: float | None
    minutes_since_warm_by: float | None
    t_rm: float | None
    t_ref: float
    cold_rm_margin: float
    last_qualifying_minutes_ago: float | None
    storm_window_min: float


def qualifies(ctx: OverrideContext, *, new_preset: str | None = None) -> str | None:
    """Return None if the override qualifies for the ledger, else the exclusion reason."""
    if ctx.dormant:
        return "dormant"
    if ctx.forecast_stale and ctx.skip_state in ("active", "aborted"):
        return "stale_forecast"
    if ctx.last_qualifying_minutes_ago is not None and ctx.last_qualifying_minutes_ago < ctx.storm_window_min:
        return "storm"
    in_affected = ctx.active_preset in ctx.affected_presets
    switching_into = new_preset is not None and new_preset in ctx.affected_presets
    if not in_affected and not switching_into:
        return "unaffected_preset"
    return None


def attribute(
    ctx: OverrideContext, direction: int, *, preheat_window_min: float = 60.0, skip_after_window_min: float = 120.0
) -> tuple[str, float]:
    """Pick the attribution bucket and weight (spec 4.6)."""
    if ctx.skip_state == "active" or (ctx.skip_ended_minutes_ago is not None and ctx.skip_ended_minutes_ago <= skip_after_window_min):
        if direction > 0:
            return BUCKET_SKIP, 2.0
    if direction > 0 and ctx.minutes_since_warm_by is not None and 0 <= ctx.minutes_since_warm_by <= preheat_window_min:
        return BUCKET_PREHEAT, 1.0
    if ctx.t_rm is not None and ctx.t_rm < ctx.t_ref - ctx.cold_rm_margin:
        return BUCKET_SLOPE, 1.0
    return BUCKET_BASELINE, 1.0


@dataclass
class BucketStats:
    """Exponentially decayed mean correction with evidence counts."""

    mean: float = 0.0
    weight: float = 0.0
    pos: float = 0.0
    neg: float = 0.0
    events: int = 0
    last_at: datetime | None = None

    def _decay(self, now: datetime, half_life: timedelta) -> None:
        if self.last_at is None or half_life.total_seconds() <= 0:
            return
        elapsed = (now - self.last_at).total_seconds()
        if elapsed <= 0:
            return
        factor = math.pow(0.5, elapsed / half_life.total_seconds())
        self.weight *= factor
        self.pos *= factor
        self.neg *= factor

    def update(self, correction: float, now: datetime, half_life: timedelta, weight: float = 1.0) -> None:
        self._decay(now, half_life)
        new_weight = self.weight + weight
        self.mean = (self.mean * self.weight + correction * weight) / new_weight if new_weight > 0 else 0.0
        self.weight = new_weight
        if correction > 0:
            self.pos += weight
        elif correction < 0:
            self.neg += weight
        self.events += 1
        self.last_at = now

    def evidence(self, now: datetime, half_life: timedelta) -> float:
        """Decayed same-direction evidence as of `now` (does not mutate)."""
        if self.last_at is None:
            return 0.0
        elapsed = max(0.0, (now - self.last_at).total_seconds())
        factor = math.pow(0.5, elapsed / half_life.total_seconds()) if half_life.total_seconds() > 0 else 1.0
        return max(self.pos, self.neg) * factor

    def correction(self, now: datetime, half_life: timedelta, *, min_events: float = 3.0, bound: float = 1.0) -> float:
        """The correction to apply: zero until evidence is consistent, then bounded.

        Requires `min_events` of decayed same-direction evidence and the mean to
        point the same way as the dominant direction.
        """
        if self.evidence(now, half_life) < min_events:
            return 0.0
        dominant = 1 if self.pos >= self.neg else -1
        if (self.mean > 0) != (dominant > 0) or self.mean == 0:
            return 0.0
        return max(-bound, min(bound, self.mean))

    def to_dict(self) -> dict:
        return {
            "mean": self.mean,
            "weight": self.weight,
            "pos": self.pos,
            "neg": self.neg,
            "events": self.events,
            "last_at": self.last_at.isoformat() if self.last_at else None,
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> BucketStats:
        if not data:
            return cls()
        return cls(
            mean=float(data.get("mean", 0.0)),
            weight=float(data.get("weight", 0.0)),
            pos=float(data.get("pos", 0.0)),
            neg=float(data.get("neg", 0.0)),
            events=int(data.get("events", 0)),
            last_at=datetime.fromisoformat(data["last_at"]) if data.get("last_at") else None,
        )


@dataclass
class Ledger:
    """The per-room correction ledger."""

    entries: list[LedgerEntry] = field(default_factory=list)
    buckets: dict[str, BucketStats] = field(default_factory=lambda: {b: BucketStats() for b in BUCKETS})
    max_entries: int = 200

    def add(self, entry: LedgerEntry, half_life: timedelta) -> None:
        self.entries.append(entry)
        del self.entries[: -self.max_entries]
        self.buckets.setdefault(entry.bucket, BucketStats()).update(entry.direction * entry.magnitude, entry.at, half_life, entry.weight)

    def reset(self, bucket: str | None = None) -> None:
        if bucket is None:
            self.entries.clear()
            self.buckets = {b: BucketStats() for b in BUCKETS}
        else:
            self.entries = [e for e in self.entries if e.bucket != bucket]
            self.buckets[bucket] = BucketStats()

    def last_entry_at(self) -> datetime | None:
        return self.entries[-1].at if self.entries else None

    def to_dict(self) -> dict:
        return {
            "entries": [e.to_dict() for e in self.entries],
            "buckets": {k: v.to_dict() for k, v in self.buckets.items()},
            "max_entries": self.max_entries,
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> Ledger:
        if not data:
            return cls()
        buckets = {b: BucketStats() for b in BUCKETS}
        for k, v in data.get("buckets", {}).items():
            buckets[k] = BucketStats.from_dict(v)
        return cls(
            entries=[LedgerEntry.from_dict(e) for e in data.get("entries", [])],
            buckets=buckets,
            max_entries=int(data.get("max_entries", 200)),
        )
