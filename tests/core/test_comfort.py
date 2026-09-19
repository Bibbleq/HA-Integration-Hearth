"""Tests for core.comfort (spec 4.1)."""

from datetime import date

import pytest

from custom_components.hearth.core.comfort import (
    ComfortResult,
    DayAccumulator,
    RunningMeanState,
    RunningMeanTracker,
    clamp,
    comfort_target,
    quantise,
    quantise_within,
    seed_running_mean,
    update_running_mean,
)


def test_clamp():
    assert clamp(5, 0, 10) == 5
    assert clamp(-1, 0, 10) == 0
    assert clamp(11, 0, 10) == 10
    assert clamp(5, 10, 0) == 10  # degenerate band collapses to lo


@pytest.mark.parametrize(
    "value,expected",
    [(20.24, 20.0), (20.25, 20.5), (20.74, 20.5), (20.75, 21.0), (-0.25, -0.5), (19.999, 20.0)],
)
def test_quantise_half_steps(value, expected):
    assert quantise(value, 0.5) == expected


def test_quantise_zero_step_is_identity():
    assert quantise(20.3, 0) == 20.3


def test_quantise_within_steps_back_inside_band():
    # 21.9 -> nearest 22.0, but band max is 21.8 -> step back to 21.5
    assert quantise_within(21.9, 0.5, 18.0, 21.8) == 21.5
    # 18.1 -> nearest 18.0, but band min is 18.2 -> step up to 18.5
    assert quantise_within(18.1, 0.5, 18.2, 22.0) == 18.5
    # ordinary case
    assert quantise_within(20.3, 0.5, 18, 22) == 20.5


def test_quantise_within_narrow_band_returns_clamped():
    # No multiple of 0.5 inside [20.1, 20.4]: the clamp wins over the quantise
    assert quantise_within(25, 0.5, 20.1, 20.4) == 20.4
    assert quantise_within(1, 0.5, 20.1, 20.4) == 20.1


def test_update_running_mean_en16798():
    # T_rm = 0.2 * T_mean + 0.8 * T_rm_prev
    assert update_running_mean(10.0, 20.0, 0.8) == pytest.approx(12.0)
    assert update_running_mean(10.0, 10.0, 0.8) == pytest.approx(10.0)


def test_seed_running_mean():
    assert seed_running_mean([], fallback=7.5) == 7.5
    assert seed_running_mean([10.0]) == 10.0
    assert seed_running_mean([10.0, 20.0], 0.8) == pytest.approx(12.0)
    assert seed_running_mean([10.0, 20.0, 20.0], 0.8) == pytest.approx(13.6)


def test_day_accumulator_roundtrip():
    acc = DayAccumulator("2026-01-01")
    assert acc.mean is None
    acc.add(4.0)
    acc.add(6.0)
    assert acc.mean == 5.0
    assert DayAccumulator.from_dict(acc.to_dict()) == acc
    assert DayAccumulator.from_dict(None) is None


def make_tracker(**kw):
    state = RunningMeanState(**kw)
    return RunningMeanTracker(state, alpha=0.8), state


def test_tracker_seed_only_once():
    tracker, state = make_tracker()
    tracker.seed(8.0, date(2026, 1, 1), "2026-01-01T00:00:00+00:00")
    tracker.seed(99.0, date(2026, 1, 2), "x")
    assert state.t_rm == 8.0
    assert state.t_rm_day == "2025-12-31"
    assert state.seeded_at == "2026-01-01T00:00:00+00:00"


def test_tracker_seed_day_samples_roll_next_day():
    tracker, state = make_tracker()
    tracker.seed(10.0, date(2026, 1, 1), "s")
    tracker.add_sample(20.0, date(2026, 1, 1))
    assert tracker.roll(date(2026, 1, 2)) is True
    assert state.t_rm == pytest.approx(12.0)
    assert state.t_rm_day == "2026-01-01"


def test_tracker_samples_then_roll():
    tracker, state = make_tracker()
    tracker.seed(10.0, date(2026, 1, 1), "s")
    d1, d2 = date(2026, 1, 2), date(2026, 1, 3)
    for v in (0.0, 10.0, 20.0):  # mean 10 on day 2 -> no change
        tracker.add_sample(v, d1)
    assert state.current.count == 3
    # First sample of day 3 promotes day 2 to completed
    tracker.add_sample(30.0, d2)
    assert state.completed.day == "2026-01-02"
    assert state.current.day == "2026-01-03"
    assert tracker.roll(d2) is True
    assert state.t_rm == pytest.approx(10.0)
    assert state.t_rm_day == "2026-01-02"
    assert state.completed is None
    # Roll again: nothing to do (idempotent)
    assert tracker.roll(d2) is False
    assert state.t_rm == pytest.approx(10.0)


def test_tracker_roll_promotes_stale_current():
    """Restart after midnight: current accumulator belongs to yesterday."""
    tracker, state = make_tracker()
    tracker.seed(10.0, date(2026, 1, 1), "s")
    tracker.add_sample(20.0, date(2026, 1, 2))
    assert tracker.roll(date(2026, 1, 3)) is True
    assert state.t_rm == pytest.approx(12.0)
    assert state.current is None
    assert state.history == [20.0]


def test_tracker_does_not_refold_old_day():
    tracker, state = make_tracker(t_rm=10.0, t_rm_day="2026-01-05")
    state.completed = DayAccumulator("2026-01-04", 40.0, 2)
    assert tracker.roll(date(2026, 1, 6)) is False
    assert state.t_rm == 10.0
    assert state.completed is None


def test_tracker_first_roll_without_seed_uses_mean():
    tracker, state = make_tracker()
    tracker.add_sample(5.0, date(2026, 1, 2))
    tracker.add_sample(7.0, date(2026, 1, 2))
    assert tracker.roll(date(2026, 1, 3)) is True
    assert state.t_rm == 6.0


def test_tracker_history_bounded():
    tracker, state = make_tracker()
    tracker.history_len = 3
    for i in range(6):
        tracker.add_sample(float(i), date(2026, 1, 1 + i))
        tracker.roll(date(2026, 1, 2 + i))
    assert len(state.history) == 3


def test_tracker_freeze_and_thaw():
    tracker, state = make_tracker()
    tracker.freeze()
    assert state.frozen
    tracker.add_sample(1.0, date(2026, 1, 1))
    assert not state.frozen


def test_state_roundtrip():
    tracker, state = make_tracker()
    tracker.seed(10.0, date(2026, 1, 1), "s")
    tracker.add_sample(3.0, date(2026, 1, 2))
    tracker.add_sample(3.0, date(2026, 1, 3))
    data = state.to_dict()
    restored = RunningMeanState.from_dict(data)
    assert restored.to_dict() == data
    assert RunningMeanState.from_dict(None).t_rm is None


def test_comfort_target_basic():
    # T_base 20.5, slope 0.15, T_rm 14 -> offset +0.6 -> 21.1 -> quantised 21.0
    r = comfort_target(20.5, 0.15, 14.0, 10.0, 18.0, 22.0, 1.5)
    assert isinstance(r, ComfortResult)
    assert r.offset_requested == pytest.approx(0.6)
    assert r.target == pytest.approx(21.1)
    assert r.quantised == 21.0
    assert not r.held


def test_comfort_target_cold_spell_lowers_nothing_beyond_band():
    r = comfort_target(20.5, 0.15, -20.0, 10.0, 18.0, 22.0, 1.5)
    # requested -4.5, capped at -1.5 -> 19.0, within band
    assert r.offset_applied == pytest.approx(-1.5)
    assert r.quantised == 19.0


def test_comfort_target_band_clamp_beats_offset_cap():
    r = comfort_target(21.5, 0.15, 25.0, 10.0, 18.0, 22.0, 1.5)
    # requested +2.25 -> capped +1.5 -> 23.0 -> clamped to 22.0
    assert r.target == 22.0
    assert r.quantised == 22.0
    assert r.offset_applied == pytest.approx(0.5)


def test_comfort_target_hold_and_missing_trm():
    r = comfort_target(20.5, 0.15, 14.0, 10.0, 18.0, 22.0, 1.5, hold=True)
    assert r.held and r.offset_requested == 0 and r.quantised == 20.5
    r = comfort_target(20.5, 0.15, None, 10.0, 18.0, 22.0, 1.5)
    assert r.held and r.quantised == 20.5


def test_comfort_target_extra_offset_cannot_escape_clamps():
    r = comfort_target(20.5, 0.15, 14.0, 10.0, 18.0, 22.0, 1.5, extra_offset=5.0)
    assert r.offset_applied == pytest.approx(1.5)
    assert r.quantised == 22.0
    r = comfort_target(20.5, 0.0, 10.0, 10.0, 18.0, 22.0, 5.0, extra_offset=-9.0)
    assert r.quantised == 18.0


def test_comfort_target_base_outside_band_is_clamped():
    r = comfort_target(25.0, 0.0, 10.0, 10.0, 18.0, 22.0, 1.5)
    assert r.quantised == 22.0
    r = comfort_target(10.0, 0.0, 10.0, 10.0, 18.0, 22.0, 1.5)
    assert r.quantised == 18.0


@pytest.mark.parametrize("t_rm", [-30, -5, 0, 5, 10, 15, 20, 30, 40])
@pytest.mark.parametrize("slope", [0.0, 0.15, 0.33, 1.0])
def test_comfort_target_never_outside_band(t_rm, slope):
    r = comfort_target(20.5, slope, t_rm, 10.0, 18.0, 22.0, 1.5)
    assert 18.0 <= r.quantised <= 22.0
    assert 18.0 <= r.target <= 22.0
