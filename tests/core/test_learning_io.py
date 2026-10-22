"""Tests for core.learning_io (D39-D44): export blob, validation, selection, merge on undo."""

import copy
import json
from datetime import UTC, datetime, timedelta

import pytest

from custom_components.hearth.core.comfort import RunningMeanState, RunningMeanTracker
from custom_components.hearth.core.learning_io import (
    FORMAT,
    PARTS,
    VERSION,
    LearningFormatError,
    build_blob,
    clamp_warming,
    ledger_has_content,
    merge_ledger,
    merge_warming,
    parse_blob,
    select_parts,
    warming_has_content,
)
from custom_components.hearth.core.override import BUCKETS, Ledger, LedgerEntry
from custom_components.hearth.core.preheat import WarmingModel

T0 = datetime(2026, 1, 10, 7, 0, tzinfo=UTC)
HALF_LIFE = timedelta(days=21)


def warming() -> WarmingModel:
    m = WarmingModel(max_runs=30)
    for delta, rate in ((10.0, 1.8), (14.0, 1.5), (18.0, 1.2)):
        m.add_run(delta, rate)
    return m


def entry(minutes: int, bucket: str, direction: int = 1, magnitude: float = 0.5) -> LedgerEntry:
    return LedgerEntry(
        at=T0 + timedelta(minutes=minutes),
        direction=direction,
        magnitude=magnitude,
        t_rm=11.0,
        t_out=6.5,
        minutes_since_preset_change=None,
        skip_state="idle",
        applied_offset=0.5,
        active_preset="comfort",
        bucket=bucket,
        weight=1.0,
        kind="temp",
    )


def ledger() -> Ledger:
    led = Ledger()
    for minutes, bucket, direction in (
        (0, "baseline_error", 1),
        (30, "preheat_shortfall", 1),
        (60, "baseline_error", -1),
        (90, "skip_failure", 1),
    ):
        led.add(entry(minutes, bucket, direction), HALF_LIFE)
    return led


def running_mean() -> RunningMeanState:
    state = RunningMeanState()
    tracker = RunningMeanTracker(state)
    tracker.seed(9.5, T0.date(), T0.isoformat())
    tracker.add_sample(8.0, T0.date())
    tracker.add_sample(10.0, T0.date())
    state.history = [9.1, 9.4]
    return state


def blob(**kwargs) -> dict:
    defaults = {
        "exported_at": T0.isoformat(),
        "hearth_version": "0.7.0",
        "room": "Living Room",
        "vtherm": "climate.living_room",
        "warming": warming(),
        "ledger": ledger(),
        "running_mean": running_mean(),
    }
    return build_blob(**{**defaults, **kwargs})


# ---------------------------------------------------------------- building


def test_blob_shape_and_content():
    b = blob()
    assert b["format"] == FORMAT == "hearth.learning" and b["version"] == VERSION == 1
    assert b["room"] == "Living Room" and b["vtherm"] == "climate.living_room" and b["hearth_version"] == "0.7.0"
    assert set(b["parts"]) == set(PARTS)


def test_part_included_only_if_it_has_content():
    b = blob(warming=WarmingModel(), ledger=Ledger(), running_mean=RunningMeanState())
    assert b["parts"] == {}
    assert not warming_has_content(WarmingModel()) and not ledger_has_content(Ledger())
    b = blob(warming=WarmingModel(), ledger=Ledger())
    assert set(b["parts"]) == {"running_mean"}


def test_parts_selection_on_export():
    assert set(blob(parts=("ledger",))["parts"]) == {"ledger"}
    assert set(blob(parts=("warming_rate", "running_mean"))["parts"]) == {"warming_rate", "running_mean"}


def test_select_parts_defaults_and_unknown():
    assert select_parts(None) == PARTS and select_parts([]) == PARTS
    assert select_parts(["running_mean", "ledger", "ledger"]) == ("ledger", "running_mean")
    with pytest.raises(LearningFormatError, match="Unknown learning part 'bogus'"):
        select_parts(["ledger", "bogus"])


# ---------------------------------------------------------------- round trips


def test_warming_round_trip():
    original = warming()
    parsed = parse_blob(blob(parts=("warming_rate",)))
    assert parsed.warming.to_dict() == original.to_dict()
    assert parsed.ledger is None and parsed.running_mean is None
    assert parsed.part_names == ("warming_rate",)


def test_ledger_round_trip():
    original = ledger()
    parsed = parse_blob(blob(parts=("ledger",)))
    assert parsed.ledger.to_dict() == original.to_dict()
    assert [e.at for e in parsed.ledger.entries] == [e.at for e in original.entries]


def test_running_mean_round_trip():
    original = running_mean()
    parsed = parse_blob(blob(parts=("running_mean",)))
    assert parsed.running_mean.to_dict() == original.to_dict()
    assert parsed.running_mean.seeded_at == original.seeded_at


def test_whole_blob_round_trip_survives_json():
    b = json.loads(json.dumps(blob()))
    parsed = parse_blob(b)
    assert parsed.part_names == PARTS
    assert parsed.vtherm == "climate.living_room" and parsed.room == "Living Room"
    assert parsed.warming.to_dict() == warming().to_dict()
    assert parsed.ledger.to_dict() == ledger().to_dict()


def test_partial_part_selection_on_parse():
    b = blob()
    parsed = parse_blob(b, ["ledger"])
    assert parsed.part_names == ("ledger",)
    # a part that is not selected is not validated
    b["parts"]["running_mean"] = {"t_rm": "warm"}
    assert parse_blob(b, ["ledger", "warming_rate"]).part_names == ("warming_rate", "ledger")
    with pytest.raises(LearningFormatError, match="running_mean.t_rm"):
        parse_blob(b)


def test_selecting_a_part_the_blob_lacks():
    with pytest.raises(LearningFormatError, match="no 'running_mean' part"):
        parse_blob(blob(running_mean=RunningMeanState()), ["running_mean"])


# ---------------------------------------------------------------- validation failures


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda b: b.update(format="something.else"), "format must be"),
        (lambda b: b.pop("format"), "format must be"),
        (lambda b: b.update(version=2), "Unsupported learning data version 2"),
        (lambda b: b.update(version=True), "Unsupported learning data version"),
        (lambda b: b.update(version="1"), "Unsupported learning data version"),
        (lambda b: b.update(parts=[]), "no 'parts' object"),
        (lambda b: b.update(parts={}), "holds no parts"),
        (lambda b: b["parts"].update(thermostat={}), "Unknown part 'thermostat'"),
        (lambda b: b.update(vtherm=5), "'vtherm' must be text"),
    ],
)
def test_structure_failures(mutate, message):
    b = blob()
    mutate(b)
    with pytest.raises(LearningFormatError, match=message):
        parse_blob(b)


@pytest.mark.parametrize("data", [None, [], "text", 3])
def test_not_an_object(data):
    with pytest.raises(LearningFormatError, match="must be an object"):
        parse_blob(data)


@pytest.mark.parametrize(
    ("part", "mutate", "message"),
    [
        ("warming_rate", lambda p: p.update(runs="many"), "warming_rate.runs must be a list"),
        ("warming_rate", lambda p: p["runs"].append([1.0]), r"runs\[3\] must be a \[delta, rate\] pair"),
        ("warming_rate", lambda p: p["runs"].append([1.0, "fast"]), r"runs\[3\]\[1\] must be a number"),
        ("warming_rate", lambda p: p["runs"].append([1.0, float("nan")]), "must be a number"),
        ("warming_rate", lambda p: p.update(a=None), "given together"),
        ("warming_rate", lambda p: p.update(b=-0.1), "warming_rate.b must be at least 0"),
        ("warming_rate", lambda p: p.update(a=True), "warming_rate.a must be a number"),
        ("warming_rate", lambda p: p.update(max_runs=0), "max_runs must be a whole number"),
        ("warming_rate", lambda p: p.update(max_runs=2.5), "max_runs must be a whole number"),
        ("ledger", lambda p: p.update(entries="none"), "ledger.entries must be a list"),
        ("ledger", lambda p: p["entries"][0].update(bucket="made_up"), r"entries\[0\].bucket must be one of"),
        ("ledger", lambda p: p["entries"][0].update(direction=2), r"entries\[0\].direction"),
        ("ledger", lambda p: p["entries"][0].update(at="yesterday"), "not a valid ISO date"),
        ("ledger", lambda p: p["entries"][0].update(at="2026-01-10T07:00:00"), "must include a UTC offset"),
        ("ledger", lambda p: p["entries"][0].update(magnitude=-1), "magnitude must be at least 0"),
        ("ledger", lambda p: p["entries"][0].pop("magnitude"), "magnitude must be a number"),
        ("ledger", lambda p: p["buckets"].update(odd_bucket={}), "unknown bucket 'odd_bucket'"),
        ("ledger", lambda p: p["buckets"]["baseline_error"].update(events=-1), "events must be a whole number"),
        ("ledger", lambda p: p["buckets"]["baseline_error"].update(last_at="2026-01-10T07:00:00"), "UTC offset"),
        ("ledger", lambda p: p.update(buckets=[]), "ledger.buckets must be an object"),
        ("running_mean", lambda p: p.update(t_rm=None), "running_mean.t_rm must be a number"),
        ("running_mean", lambda p: p.update(seeded_at=None), "seeded_at must be an ISO date"),
        ("running_mean", lambda p: p.update(t_rm_day="10/01/2026"), "t_rm_day must be an ISO date"),
        ("running_mean", lambda p: p.update(frozen="yes"), "frozen must be true or false"),
        ("running_mean", lambda p: p.update(history="cold"), "history must be a list"),
        ("running_mean", lambda p: p.update(history=[1.0, "x"]), r"history\[1\] must be a number"),
        ("running_mean", lambda p: p["current"].update(count=-3), "current.count must be a whole number"),
        ("running_mean", lambda p: p.update(current="today"), "running_mean.current must be an object"),
    ],
)
def test_part_shape_failures(part, mutate, message):
    b = blob()
    parse_blob(b)  # the untouched blob is good
    mutate(b["parts"][part])
    with pytest.raises(LearningFormatError, match=message):
        parse_blob(b)


def test_a_missing_fit_is_computed_from_the_runs():
    b = blob()
    b["parts"]["warming_rate"].update(a=None, b=None)
    parsed = parse_blob(b, ["warming_rate"])
    assert parsed.warming.a == pytest.approx(warming().a) and parsed.warming.b == pytest.approx(warming().b)


def test_parse_does_not_alias_the_blob():
    b = blob()
    snapshot = copy.deepcopy(b)
    parsed = parse_blob(b)
    parsed.warming.runs.append((1.0, 1.0))
    parsed.ledger.entries.clear()
    assert b == snapshot


# ---------------------------------------------------------------- applying and merging


def test_clamp_warming_trims_and_refits_only_when_needed():
    model = WarmingModel(max_runs=30)
    for delta, rate in ((10.0, 1.9), (14.0, 1.3), (18.0, 1.25)):  # deliberately not on a line
        model.add_run(delta, rate)
    before = model.to_dict()
    assert clamp_warming(WarmingModel.from_dict(before), 30).to_dict() == before
    clamp_warming(model, 2)
    assert model.max_runs == 2 and model.runs == [(14.0, 1.3), (18.0, 1.25)]
    # refitted on the two kept runs, not left on the three-run constants
    assert (model.a, model.b) != (before["a"], before["b"])
    assert model.a - model.b * 14.0 == pytest.approx(1.3)


def test_merge_warming_without_newer_runs_is_exact():
    stashed = warming()
    restored = merge_warming(WarmingModel(max_runs=30), stashed, 30)
    assert restored.to_dict() == stashed.to_dict()
    assert restored is not stashed


def test_merge_warming_keeps_runs_learnt_since():
    stashed = warming()
    current = WarmingModel(max_runs=30)
    current.add_run(12.0, 1.6)
    restored = merge_warming(current, stashed, 30)
    assert restored.runs == [*stashed.runs, (12.0, 1.6)]
    assert restored.n_runs == 4 and restored.a is not None


def test_merge_ledger_restores_one_bucket_and_leaves_the_rest():
    full = ledger()
    current = Ledger.from_dict(full.to_dict())
    current.reset("baseline_error")
    restored = merge_ledger(current, full, ("baseline_error",), HALF_LIFE)
    assert restored.to_dict() == full.to_dict()


def test_merge_ledger_keeps_other_buckets_changed_since_the_reset():
    full = ledger()
    current = Ledger.from_dict(full.to_dict())
    current.reset("baseline_error")
    current.add(entry(200, "preheat_shortfall"), HALF_LIFE)
    restored = merge_ledger(current, full, ("baseline_error",), HALF_LIFE)
    assert [e.bucket for e in restored.entries].count("preheat_shortfall") == 2
    assert restored.buckets["preheat_shortfall"].to_dict() == current.buckets["preheat_shortfall"].to_dict()
    assert restored.buckets["baseline_error"].to_dict() == full.buckets["baseline_error"].to_dict()
    assert [e.at for e in restored.entries] == sorted(e.at for e in restored.entries)


def test_merge_ledger_replays_entries_recorded_in_the_restored_bucket():
    full = ledger()
    current = Ledger()
    new = entry(500, "baseline_error", direction=-1, magnitude=0.25)
    current.add(new, HALF_LIFE)
    restored = merge_ledger(current, full, BUCKETS, HALF_LIFE)
    expected = Ledger.from_dict(full.to_dict())
    expected.add(new, HALF_LIFE)
    assert restored.buckets["baseline_error"].to_dict() == expected.buckets["baseline_error"].to_dict()
    assert restored.buckets["baseline_error"].events == 3
    assert len(restored.entries) == len(full.entries) + 1


def test_merge_ledger_all_buckets_after_reset_all_is_exact():
    full = ledger()
    restored = merge_ledger(Ledger(), full, BUCKETS, HALF_LIFE)
    assert restored.to_dict() == full.to_dict()
