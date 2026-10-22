"""Export, import and restore of learnt values (D39-D44). Pure: no Home Assistant imports.

A learning blob is a plain JSON-able dict::

    {"format": "hearth.learning", "version": 1, "exported_at": ..., "hearth_version": ..., "room": ...,
     "vtherm": ..., "parts": {"warming_rate": {...}, "ledger": {...}, "running_mean": {...}}}

Each part is exactly the `to_dict()` of the model it carries, so a part can also be lifted out of a
diagnostics download. Parsing validates everything and builds fresh model objects; nothing here touches
live state, so a caller can parse first and apply only once the whole blob is known to be good.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from .comfort import DayAccumulator, RunningMeanState
from .override import BUCKETS, BucketStats, Ledger, LedgerEntry
from .preheat import WarmingModel, fit

FORMAT = "hearth.learning"
VERSION = 1

PART_WARMING = "warming_rate"
PART_LEDGER = "ledger"
PART_RUNNING_MEAN = "running_mean"
PARTS = (PART_WARMING, PART_LEDGER, PART_RUNNING_MEAN)


class LearningFormatError(ValueError):
    """The blob or a request for parts is not usable. The message is shown to the user."""


# ---------------------------------------------------------------- content tests


def warming_has_content(model: WarmingModel) -> bool:
    return bool(model.runs) or model.a is not None


def bucket_has_content(ledger: Ledger, bucket: str) -> bool:
    stats = ledger.buckets.get(bucket)
    return any(e.bucket == bucket for e in ledger.entries) or (stats is not None and (stats.events > 0 or stats.weight > 0))


def ledger_has_content(ledger: Ledger) -> bool:
    return bool(ledger.entries) or any(bucket_has_content(ledger, b) for b in ledger.buckets)


def running_mean_has_content(state: RunningMeanState) -> bool:
    return state.t_rm is not None


# ---------------------------------------------------------------- selection and building


def select_parts(requested: list[str] | tuple[str, ...] | None, default: tuple[str, ...] = PARTS) -> tuple[str, ...]:
    """Validate a `parts` request. Empty or missing means `default`. Order and duplicates are normalised."""
    if not requested:
        return default
    unknown = [p for p in requested if p not in PARTS]
    if unknown:
        raise LearningFormatError(f"Unknown learning part {', '.join(repr(p) for p in unknown)}; expected any of {', '.join(PARTS)}")
    return tuple(p for p in PARTS if p in requested)


def build_blob(
    *,
    exported_at: str,
    hearth_version: str | None,
    room: str,
    vtherm: str,
    parts: tuple[str, ...] = PARTS,
    warming: WarmingModel | None = None,
    ledger: Ledger | None = None,
    running_mean: RunningMeanState | None = None,
) -> dict:
    """Build the export blob. A part is included only if it was asked for and holds something."""
    out: dict[str, Any] = {}
    if PART_WARMING in parts and warming is not None and warming_has_content(warming):
        out[PART_WARMING] = warming.to_dict()
    if PART_LEDGER in parts and ledger is not None and ledger_has_content(ledger):
        out[PART_LEDGER] = ledger.to_dict()
    if PART_RUNNING_MEAN in parts and running_mean is not None and running_mean_has_content(running_mean):
        out[PART_RUNNING_MEAN] = running_mean.to_dict()
    return {
        "format": FORMAT,
        "version": VERSION,
        "exported_at": exported_at,
        "hearth_version": hearth_version,
        "room": room,
        "vtherm": vtherm,
        "parts": out,
    }


# ---------------------------------------------------------------- parsing


@dataclass
class ParsedLearning:
    """A validated blob, with the selected parts built into fresh model objects."""

    vtherm: str | None
    room: str | None
    exported_at: str | None
    warming: WarmingModel | None = None
    ledger: Ledger | None = None
    running_mean: RunningMeanState | None = None

    @property
    def part_names(self) -> tuple[str, ...]:
        return tuple(
            name
            for name, value in ((PART_WARMING, self.warming), (PART_LEDGER, self.ledger), (PART_RUNNING_MEAN, self.running_mean))
            if value is not None
        )


def parse_blob(data: Any, parts: list[str] | tuple[str, ...] | None = None) -> ParsedLearning:
    """Validate a blob and build the selected parts (default: every part it holds).

    Raises LearningFormatError on anything wrong. The structure (format, version, part names) is always
    checked; the shape of a part is checked only when that part is selected.
    """
    if not isinstance(data, dict):
        raise LearningFormatError("The learning data must be an object, as returned by hearth.export_learning")
    if data.get("format") != FORMAT:
        raise LearningFormatError(f"Not Hearth learning data: format must be {FORMAT!r}, got {data.get('format')!r}")
    version = data.get("version")
    if isinstance(version, bool) or version != VERSION:
        raise LearningFormatError(f"Unsupported learning data version {version!r}; this Hearth reads version {VERSION}")
    present = data.get("parts")
    if not isinstance(present, dict):
        raise LearningFormatError("The learning data has no 'parts' object")
    unknown = [p for p in present if p not in PARTS]
    if unknown:
        raise LearningFormatError(
            f"Unknown part {', '.join(repr(p) for p in unknown)} in the learning data; expected any of {', '.join(PARTS)}"
        )
    if not present:
        raise LearningFormatError("The learning data holds no parts to import")
    wanted = select_parts(parts, tuple(p for p in PARTS if p in present))
    missing = [p for p in wanted if p not in present]
    if missing:
        raise LearningFormatError(f"The learning data has no {', '.join(repr(p) for p in missing)} part (it holds: {', '.join(present)})")

    parsed = ParsedLearning(
        vtherm=_opt_str(data.get("vtherm"), "vtherm"),
        room=_opt_str(data.get("room"), "room"),
        exported_at=_opt_str(data.get("exported_at"), "exported_at"),
    )
    if PART_WARMING in wanted:
        parsed.warming = _parse_warming(present[PART_WARMING])
    if PART_LEDGER in wanted:
        parsed.ledger = _parse_ledger(present[PART_LEDGER])
    if PART_RUNNING_MEAN in wanted:
        parsed.running_mean = _parse_running_mean(present[PART_RUNNING_MEAN])
    return parsed


def _opt_str(value: Any, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise LearningFormatError(f"{name!r} must be text")
    return value


def _num(value: Any, where: str, *, none_ok: bool = False, minimum: float | None = None) -> float | None:
    if value is None and none_ok:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise LearningFormatError(f"{where} must be a number")
    if minimum is not None and value < minimum:
        raise LearningFormatError(f"{where} must be at least {minimum:g}")
    return float(value)


def _int(value: Any, where: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise LearningFormatError(f"{where} must be a whole number of at least {minimum}")
    return value


def _aware(value: Any, where: str, *, none_ok: bool = False) -> datetime | None:
    if value is None and none_ok:
        return None
    if not isinstance(value, str):
        raise LearningFormatError(f"{where} must be an ISO date and time")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as err:
        raise LearningFormatError(f"{where} is not a valid ISO date and time: {value!r}") from err
    if parsed.tzinfo is None:
        raise LearningFormatError(f"{where} must include a UTC offset: {value!r}")
    return parsed


def _day(value: Any, where: str, *, none_ok: bool = False) -> str | None:
    if value is None and none_ok:
        return None
    try:
        if not isinstance(value, str):
            raise ValueError
        date.fromisoformat(value)
    except ValueError as err:
        raise LearningFormatError(f"{where} must be an ISO date (YYYY-MM-DD)") from err
    return value


def _dict(value: Any, where: str) -> dict:
    if not isinstance(value, dict):
        raise LearningFormatError(f"{where} must be an object")
    return value


def _parse_warming(raw: Any) -> WarmingModel:
    raw = _dict(raw, "warming_rate")
    runs_raw = raw.get("runs")
    if not isinstance(runs_raw, list):
        raise LearningFormatError("warming_rate.runs must be a list of [delta, rate] pairs")
    runs: list[tuple[float, float]] = []
    for i, run in enumerate(runs_raw):
        if not isinstance(run, list | tuple) or len(run) != 2:
            raise LearningFormatError(f"warming_rate.runs[{i}] must be a [delta, rate] pair")
        runs.append((_num(run[0], f"warming_rate.runs[{i}][0]"), _num(run[1], f"warming_rate.runs[{i}][1]")))  # type: ignore[arg-type]
    a = _num(raw.get("a"), "warming_rate.a", none_ok=True)
    b = _num(raw.get("b"), "warming_rate.b", none_ok=True, minimum=0.0)
    if (a is None) != (b is None):
        raise LearningFormatError("warming_rate.a and warming_rate.b must be given together")
    if a is None and runs:
        a, b = fit(runs)
    max_runs = _int(raw.get("max_runs", 30), "warming_rate.max_runs", minimum=1)
    return WarmingModel(a=a, b=b, runs=runs, max_runs=max_runs)


def _parse_ledger(raw: Any) -> Ledger:
    raw = _dict(raw, "ledger")
    entries_raw = raw.get("entries", [])
    buckets_raw = _dict(raw.get("buckets", {}), "ledger.buckets")
    if not isinstance(entries_raw, list):
        raise LearningFormatError("ledger.entries must be a list")
    max_entries = _int(raw.get("max_entries", 200), "ledger.max_entries", minimum=1)
    entries: list[LedgerEntry] = []
    for i, item in enumerate(entries_raw):
        where = f"ledger.entries[{i}]"
        item = _dict(item, where)
        at = _aware(item.get("at"), f"{where}.at")
        bucket = item.get("bucket")
        if bucket not in BUCKETS:
            raise LearningFormatError(f"{where}.bucket must be one of {', '.join(BUCKETS)}")
        direction = item.get("direction")
        if direction not in (1, -1) or isinstance(direction, bool):
            raise LearningFormatError(f"{where}.direction must be 1 or -1")
        for name in ("t_rm", "t_out", "minutes_since_preset_change"):
            _num(item.get(name), f"{where}.{name}", none_ok=True)
        try:
            entries.append(
                LedgerEntry(
                    at=at,  # type: ignore[arg-type]
                    direction=int(direction),
                    magnitude=_num(item.get("magnitude"), f"{where}.magnitude", minimum=0.0),  # type: ignore[arg-type]
                    t_rm=item.get("t_rm"),
                    t_out=item.get("t_out"),
                    minutes_since_preset_change=item.get("minutes_since_preset_change"),
                    skip_state=str(item.get("skip_state", "idle")),
                    applied_offset=_num(item.get("applied_offset", 0.0), f"{where}.applied_offset"),  # type: ignore[arg-type]
                    active_preset=item.get("active_preset") if isinstance(item.get("active_preset"), str) else None,
                    bucket=bucket,
                    weight=_num(item.get("weight", 1.0), f"{where}.weight", minimum=0.0),  # type: ignore[arg-type]
                    kind=str(item.get("kind", "temp")),
                )
            )
        except (TypeError, ValueError) as err:
            raise LearningFormatError(f"{where} is malformed: {err}") from err
    buckets = {b: BucketStats() for b in BUCKETS}
    for name, stats in buckets_raw.items():
        if name not in BUCKETS:
            raise LearningFormatError(f"ledger.buckets has unknown bucket {name!r}; expected any of {', '.join(BUCKETS)}")
        where = f"ledger.buckets.{name}"
        stats = _dict(stats, where)
        buckets[name] = BucketStats(
            mean=_num(stats.get("mean", 0.0), f"{where}.mean"),  # type: ignore[arg-type]
            weight=_num(stats.get("weight", 0.0), f"{where}.weight", minimum=0.0),  # type: ignore[arg-type]
            pos=_num(stats.get("pos", 0.0), f"{where}.pos", minimum=0.0),  # type: ignore[arg-type]
            neg=_num(stats.get("neg", 0.0), f"{where}.neg", minimum=0.0),  # type: ignore[arg-type]
            events=_int(stats.get("events", 0), f"{where}.events"),
            last_at=_aware(stats.get("last_at"), f"{where}.last_at", none_ok=True),
        )
    del entries[:-max_entries]
    return Ledger(entries=entries, buckets=buckets, max_entries=max_entries)


def _parse_accumulator(raw: Any, where: str) -> DayAccumulator | None:
    if raw is None:
        return None
    raw = _dict(raw, where)
    return DayAccumulator(
        day=_day(raw.get("day"), f"{where}.day"),  # type: ignore[arg-type]
        total=_num(raw.get("total", 0.0), f"{where}.total"),  # type: ignore[arg-type]
        count=_int(raw.get("count", 0), f"{where}.count"),
    )


def _parse_running_mean(raw: Any) -> RunningMeanState:
    raw = _dict(raw, "running_mean")
    t_rm = _num(raw.get("t_rm"), "running_mean.t_rm")
    seeded_at = raw.get("seeded_at")
    _aware(seeded_at, "running_mean.seeded_at")  # the state carries its own seed time, so it must have one
    history_raw = raw.get("history", [])
    if not isinstance(history_raw, list):
        raise LearningFormatError("running_mean.history must be a list of daily means")
    frozen = raw.get("frozen", False)
    if not isinstance(frozen, bool):
        raise LearningFormatError("running_mean.frozen must be true or false")
    return RunningMeanState(
        t_rm=t_rm,
        t_rm_day=_day(raw.get("t_rm_day"), "running_mean.t_rm_day", none_ok=True),
        seeded_at=seeded_at,
        frozen=frozen,
        current=_parse_accumulator(raw.get("current"), "running_mean.current"),
        completed=_parse_accumulator(raw.get("completed"), "running_mean.completed"),
        history=[_num(x, f"running_mean.history[{i}]") for i, x in enumerate(history_raw)],  # type: ignore[misc]
    )


# ---------------------------------------------------------------- applying


def clamp_warming(model: WarmingModel, max_runs: int) -> WarmingModel:
    """Apply the room's run limit to an imported model, refitting only if runs had to be dropped."""
    model.max_runs = max_runs
    if len(model.runs) > max_runs:
        del model.runs[:-max_runs]
        model.a, model.b = fit(model.runs)
    return model


def merge_warming(current: WarmingModel, stashed: WarmingModel, max_runs: int) -> WarmingModel:
    """Undo a warming-rate reset: the stashed runs, then anything learnt since.

    With nothing learnt since the reset the stashed model comes back as it was, constants included.
    """
    if not current.runs:
        return clamp_warming(WarmingModel.from_dict(stashed.to_dict()), max_runs)
    merged = WarmingModel(runs=[*stashed.runs, *current.runs], max_runs=max_runs)
    del merged.runs[:-max_runs]
    merged.a, merged.b = fit(merged.runs)
    return merged


def merge_ledger(current: Ledger, stashed: Ledger, buckets: tuple[str, ...], half_life: timedelta) -> Ledger:
    """Undo a ledger reset: put `buckets` back from the stash without disturbing the other buckets.

    Entries recorded in a restored bucket since the reset are kept: they are replayed onto the stashed
    statistics with the same update the ledger uses when it records them. With nothing recorded since,
    the stashed bucket (entries and statistics) comes back exactly as it was.
    """
    result = Ledger.from_dict(current.to_dict())
    kept = [e for e in current.entries if e.bucket not in buckets]
    restored: list[LedgerEntry] = []
    for bucket in buckets:
        newer = [e for e in current.entries if e.bucket == bucket]
        restored.extend(e for e in stashed.entries if e.bucket == bucket)
        restored.extend(newer)
        stats = BucketStats.from_dict(stashed.buckets[bucket].to_dict()) if bucket in stashed.buckets else BucketStats()
        for entry in newer:
            stats.update(entry.direction * entry.magnitude, entry.at, half_life, entry.weight)
        result.buckets[bucket] = stats
    result.entries = sorted([*kept, *restored], key=lambda e: e.at)
    del result.entries[: -result.max_entries]
    return result
