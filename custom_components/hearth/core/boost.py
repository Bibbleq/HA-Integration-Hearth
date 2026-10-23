"""Boost evidence for the schedule (D60).

A timed boost while the schedule has the room on eco or frost says somebody is using the room when the schedule thinks
it is empty. One boost is noise; the same hour on the same kind of day, again and again, is a schedule worth changing.
Hearth never edits the schedule: it only reports the pattern.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

WORKDAY = "workday"
NON_WORKDAY = "non_workday"


@dataclass(frozen=True)
class BoostEvent:
    at: datetime
    day_type: str
    hour: int
    preset: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"at": self.at.isoformat(), "day_type": self.day_type, "hour": self.hour, "preset": self.preset}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BoostEvent:
        return cls(datetime.fromisoformat(data["at"]), str(data["day_type"]), int(data["hour"]), data.get("preset"))


def prune(events: list[BoostEvent], now: datetime, window_days: float) -> list[BoostEvent]:
    cutoff = now - timedelta(days=window_days)
    return [e for e in events if e.at >= cutoff]


def suggestions(events: list[BoostEvent], now: datetime, window_days: float, min_events: int) -> list[dict[str, Any]]:
    """(day type, hour) slots boosted at least `min_events` times within the window, busiest first."""
    recent = prune(events, now, window_days)
    counts = Counter((e.day_type, e.hour) for e in recent)
    out = []
    for (day_type, hour), count in counts.items():
        if count < min_events:
            continue
        in_slot = [e for e in recent if e.day_type == day_type and e.hour == hour]
        out.append(
            {
                "day_type": day_type,
                "hour": hour,
                "slot": f"{hour:02d}:00-{(hour + 1) % 24:02d}:00",
                "count": count,
                "last_at": max(e.at for e in in_slot).isoformat(),
                "presets": sorted({e.preset for e in in_slot if e.preset}),
            }
        )
    return sorted(out, key=lambda s: (-s["count"], s["day_type"], s["hour"]))
