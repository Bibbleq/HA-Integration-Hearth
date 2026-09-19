"""Tests for core.override (spec 4.6)."""

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.hearth.core.override import (
    BUCKET_BASELINE,
    BUCKET_PREHEAT,
    BUCKET_SKIP,
    BUCKET_SLOPE,
    BUCKETS,
    BucketStats,
    Ledger,
    LedgerEntry,
    OverrideContext,
    StandDown,
    WriteLog,
    attribute,
    qualifies,
    standdown_until,
)

UTC = UTC
T0 = datetime(2026, 1, 10, 18, 0, tzinfo=UTC)
HL = timedelta(days=21)


def test_write_log_matching():
    log = WriteLog(max_len=3)
    log.record("number.x", 20.5, T0, "adaptive")
    assert log.made_by_us("number.x", "20.5", T0 + timedelta(seconds=5), timedelta(minutes=2))
    assert log.made_by_us("number.x", 20.50, T0 + timedelta(seconds=5), timedelta(minutes=2))
    assert not log.made_by_us("number.x", 21.0, T0 + timedelta(seconds=5), timedelta(minutes=2))
    assert not log.made_by_us("number.y", 20.5, T0 + timedelta(seconds=5), timedelta(minutes=2))
    assert not log.made_by_us("number.x", 20.5, T0 + timedelta(minutes=3), timedelta(minutes=2))  # too old
    log.record("climate.r", "eco", T0 + timedelta(minutes=1), "skip")
    assert log.made_by_us("climate.r", "eco", T0 + timedelta(minutes=1, seconds=1), timedelta(minutes=2))
    assert log.last_for("climate.r").value == "eco"
    assert log.last_for("nothing") is None


def test_write_log_bounded_and_roundtrip():
    log = WriteLog(max_len=2)
    for i in range(5):
        log.record("e", i, T0 + timedelta(seconds=i))
    assert len(log.records) == 2
    data = log.to_list()
    assert WriteLog.from_list(data, 2).to_list() == data


def test_standdown_until():
    timeout = timedelta(minutes=180)
    assert standdown_until(T0, None, timeout) == T0 + timeout
    assert standdown_until(T0, T0 + timedelta(minutes=30), timeout) == T0 + timedelta(minutes=30)
    assert standdown_until(T0, T0 + timedelta(minutes=300), timeout) == T0 + timeout
    assert standdown_until(T0, T0 - timedelta(minutes=5), timeout) == T0 + timeout  # boundary in the past


def test_standdown_state():
    sd = StandDown()
    assert not sd.active(T0)
    sd = StandDown(until=T0 + timedelta(hours=1), cause="manual", started_at=T0)
    assert sd.active(T0 + timedelta(minutes=59))
    assert not sd.active(T0 + timedelta(hours=1))
    data = sd.to_dict()
    assert StandDown.from_dict(data).to_dict() == data
    sd.clear()
    assert sd.until is None and StandDown.from_dict(sd.to_dict()).until is None


def ctx(**over):
    kw = dict(
        dormant=False,
        forecast_stale=False,
        active_preset="comfort",
        affected_presets=("comfort",),
        skip_state="idle",
        skip_ended_minutes_ago=None,
        minutes_since_warm_by=None,
        t_rm=10.0,
        t_ref=10.0,
        cold_rm_margin=3.0,
        last_qualifying_minutes_ago=None,
        storm_window_min=180.0,
    )
    kw.update(over)
    return OverrideContext(**kw)


def test_qualifies_exclusions():
    assert qualifies(ctx()) is None
    assert qualifies(ctx(dormant=True)) == "dormant"
    assert qualifies(ctx(forecast_stale=True, skip_state="active")) == "stale_forecast"
    assert qualifies(ctx(forecast_stale=True, skip_state="idle")) is None
    assert qualifies(ctx(last_qualifying_minutes_ago=30.0)) == "storm"
    assert qualifies(ctx(last_qualifying_minutes_ago=200.0)) is None
    assert qualifies(ctx(active_preset="eco")) == "unaffected_preset"
    assert qualifies(ctx(active_preset="eco"), new_preset="comfort") is None
    assert qualifies(ctx(active_preset="eco"), new_preset="frost") == "unaffected_preset"


def test_attribute_buckets():
    assert attribute(ctx(skip_state="active"), +1) == (BUCKET_SKIP, 2.0)
    assert attribute(ctx(skip_state="aborted", skip_ended_minutes_ago=60.0), +1) == (BUCKET_SKIP, 2.0)
    assert attribute(ctx(skip_state="aborted", skip_ended_minutes_ago=600.0), +1) == (BUCKET_BASELINE, 1.0)
    # Turning it down during a skip is not a skip failure
    assert attribute(ctx(skip_state="active"), -1) == (BUCKET_BASELINE, 1.0)
    assert attribute(ctx(minutes_since_warm_by=30.0), +1) == (BUCKET_PREHEAT, 1.0)
    assert attribute(ctx(minutes_since_warm_by=90.0), +1) == (BUCKET_BASELINE, 1.0)
    assert attribute(ctx(minutes_since_warm_by=30.0), -1) == (BUCKET_BASELINE, 1.0)
    assert attribute(ctx(t_rm=2.0), +1) == (BUCKET_SLOPE, 1.0)
    assert attribute(ctx(t_rm=2.0), -1) == (BUCKET_SLOPE, 1.0)
    assert attribute(ctx(t_rm=8.0), +1) == (BUCKET_BASELINE, 1.0)
    # Skip beats preheat when both apply
    assert attribute(ctx(skip_state="active", minutes_since_warm_by=10.0), +1)[0] == BUCKET_SKIP


def test_bucket_stats_requires_consistent_evidence():
    s = BucketStats()
    assert s.correction(T0, HL) == 0.0
    s.update(0.5, T0, HL)
    s.update(0.5, T0, HL)
    assert s.correction(T0, HL) == 0.0  # only 2 events
    s.update(1.0, T0, HL)
    c = s.correction(T0, HL)
    assert c == pytest.approx(2.0 / 3.0)
    assert s.events == 3
    # A fourth event an hour later still clears the bar despite the tiny decay
    s.update(1.0, T0 + timedelta(hours=1), HL)
    assert s.correction(T0 + timedelta(hours=1), HL) == pytest.approx(0.75, rel=1e-3)


def test_bucket_stats_mixed_direction_gives_zero():
    s = BucketStats()
    for i, v in enumerate((1.0, -1.0, 1.0, -1.0, 1.0, -1.0)):
        s.update(v, T0 + timedelta(hours=i), HL)
    # mean 0 -> no correction
    assert s.correction(T0 + timedelta(hours=6), HL) == 0.0
    s2 = BucketStats()
    for i, v in enumerate((2.0, 2.0, 2.0, -1.0, -1.0, -1.0, -1.0)):
        s2.update(v, T0 + timedelta(hours=i), HL)
    # dominant direction negative (4 vs 3) but mean positive -> inconsistent -> 0
    assert s2.correction(T0 + timedelta(hours=7), HL) == 0.0


def test_bucket_stats_bounded():
    s = BucketStats()
    for i in range(4):
        s.update(3.0, T0 + timedelta(hours=i), HL)
    assert s.correction(T0 + timedelta(hours=4), HL, bound=1.0) == 1.0
    s = BucketStats()
    for i in range(4):
        s.update(-3.0, T0 + timedelta(hours=i), HL)
    assert s.correction(T0 + timedelta(hours=4), HL, bound=1.0) == -1.0


def test_bucket_stats_decay():
    s = BucketStats()
    for _ in range(3):
        s.update(1.0, T0, HL)
    assert s.evidence(T0, HL) == pytest.approx(3.0)
    assert s.evidence(T0 + HL, HL) == pytest.approx(1.5)
    # After one half-life the evidence drops below 3 and the correction switches off
    assert s.correction(T0 + HL, HL) == 0.0
    # Adding a new event decays the old weight before merging
    s.update(0.0, T0 + HL, HL)
    assert s.weight == pytest.approx(2.5)
    assert s.mean == pytest.approx(1.5 / 2.5)


def test_bucket_stats_weighting_and_roundtrip():
    s = BucketStats()
    s.update(1.0, T0, HL, weight=2.0)
    s.update(0.0, T0, HL, weight=1.0)
    assert s.mean == pytest.approx(2.0 / 3.0)
    assert s.pos == 2.0 and s.neg == 0.0
    data = s.to_dict()
    assert BucketStats.from_dict(data).to_dict() == data
    assert BucketStats.from_dict(None).events == 0


def entry(bucket, direction=1, magnitude=0.5, at=T0, weight=1.0):
    return LedgerEntry(at, direction, magnitude, 10.0, 5.0, 30.0, "idle", 0.5, "comfort", bucket, weight)


def test_ledger_add_reset_roundtrip():
    ledger = Ledger(max_entries=3)
    for i in range(4):
        ledger.add(entry(BUCKET_BASELINE, at=T0 + timedelta(hours=i)), HL)
    ledger.add(entry(BUCKET_SKIP, weight=2.0, at=T0 + timedelta(hours=5)), HL)
    assert len(ledger.entries) == 3
    assert ledger.buckets[BUCKET_BASELINE].events == 4
    assert ledger.buckets[BUCKET_SKIP].weight == 2.0
    assert ledger.last_entry_at() == T0 + timedelta(hours=5)
    data = ledger.to_dict()
    restored = Ledger.from_dict(data)
    assert restored.to_dict() == data
    assert set(restored.buckets) == set(BUCKETS)
    ledger.reset(BUCKET_SKIP)
    assert ledger.buckets[BUCKET_SKIP].events == 0
    assert ledger.buckets[BUCKET_BASELINE].events == 4
    assert all(e.bucket != BUCKET_SKIP for e in ledger.entries)
    ledger.reset()
    assert not ledger.entries and all(b.events == 0 for b in ledger.buckets.values())
    assert Ledger.from_dict(None).last_entry_at() is None


def test_ledger_entry_roundtrip():
    e = entry(BUCKET_SLOPE, -1, 1.0)
    assert LedgerEntry.from_dict(e.to_dict()) == e
