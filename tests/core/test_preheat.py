"""Tests for core.preheat (spec 4.4)."""

from datetime import datetime, timedelta, timezone

import pytest

from custom_components.hearth.core.preheat import HeatingRun, WarmingModel, fit, lead_minutes, preheat_start, warming_rate

UTC = timezone.utc
T0 = datetime(2026, 1, 10, 5, 0, tzinfo=UTC)


def test_fit_recovers_linear_model():
    a, b = 2.0, 0.05
    runs = [(d, a - b * d) for d in (5.0, 10.0, 15.0, 20.0, 25.0)]
    fa, fb = fit(runs)
    assert fa == pytest.approx(a) and fb == pytest.approx(b)


def test_fit_degenerate_cases():
    assert fit([]) == (None, None)
    assert fit([(10.0, 1.2)]) == (1.2, 0.0)
    assert fit([(10.0, 1.0), (10.0, 2.0)]) == (1.5, 0.0)


def test_fit_forces_non_negative_b():
    # Rate rising with deficit is physically wrong: b clamps to 0, a = mean rate
    runs = [(5.0, 1.0), (10.0, 1.5), (15.0, 2.0)]
    a, b = fit(runs)
    assert b == 0.0 and a == pytest.approx(1.5)


def test_model_add_run_refits_and_bounds():
    m = WarmingModel(max_runs=3)
    for d in (5.0, 10.0, 15.0, 20.0):
        m.add_run(d, 2.0 - 0.05 * d)
    assert m.n_runs == 3
    assert m.a == pytest.approx(2.0) and m.b == pytest.approx(0.05)
    data = m.to_dict()
    assert WarmingModel.from_dict(data).to_dict() == data
    m.reset()
    assert m.n_runs == 0 and m.a is None


def test_warming_rate_cold_start_uses_default():
    m = WarmingModel()
    rate, learned = warming_rate(m, 20.0, 5.0)
    assert rate == 1.0 and not learned
    for d in (5.0, 10.0, 15.0, 20.0):
        m.add_run(d, 2.0 - 0.05 * d)
    rate, learned = warming_rate(m, 20.0, 5.0, min_runs=5)
    assert rate == 1.0 and not learned  # only 4 runs
    m.add_run(25.0, 2.0 - 0.05 * 25.0)
    rate, learned = warming_rate(m, 20.0, 5.0, min_runs=5)
    assert learned and rate == pytest.approx(2.0 - 0.05 * 15.0)


def test_warming_rate_clamped_and_needs_outdoor():
    m = WarmingModel(a=10.0, b=0.0, runs=[(1.0, 10.0)] * 5)
    assert warming_rate(m, 20.0, 5.0)[0] == 3.0
    m = WarmingModel(a=0.1, b=0.0, runs=[(1.0, 0.1)] * 5)
    assert warming_rate(m, 20.0, 5.0)[0] == 0.3
    assert warming_rate(m, 20.0, None) == (1.0, False)


def test_lead_minutes():
    # 2C deficit at 1 C/h * 1.15 = 138 min -> capped at 120
    assert lead_minutes(2.0, 1.0, safety_factor=1.15, max_preheat_min=120) == 120
    assert lead_minutes(1.0, 1.0, safety_factor=1.15, max_preheat_min=120) == pytest.approx(69.0)
    assert lead_minutes(0.0, 1.0) == 0.0
    assert lead_minutes(-1.0, 1.0) == 0.0
    assert lead_minutes(1.0, 0.0) == 0.0


def test_preheat_start():
    assert preheat_start(T0, 30) == T0 - timedelta(minutes=30)


def test_heating_run_result_clean():
    run = HeatingRun(T0, 20.0, 17.0, 17.0, T0)
    run.sample(T0 + timedelta(minutes=30), 18.0, 5.0)
    run.sample(T0 + timedelta(minutes=60), 19.0, 7.0)
    delta, rate = run.result()
    assert rate == pytest.approx(2.0)
    assert delta == pytest.approx(20.0 - 6.0)


def test_heating_run_rejects_short_small_or_tainted():
    run = HeatingRun(T0, 20.0, 17.0, 17.0, T0)
    run.sample(T0 + timedelta(minutes=10), 18.0, 5.0)
    assert run.result(min_minutes=20) is None  # too short
    run.sample(T0 + timedelta(minutes=30), 17.1, 5.0)
    assert run.result(min_rise=0.3) is None  # too small a rise
    run.sample(T0 + timedelta(minutes=40), 18.0, 5.0)
    assert run.result() is not None
    run.taint("window")
    assert run.result() is None and run.taint_reason == "window"
    # No outdoor samples at all -> unusable
    run2 = HeatingRun(T0, 20.0, 17.0, 18.5, T0 + timedelta(hours=1))
    assert run2.result() is None


def test_heating_run_roundtrip():
    run = HeatingRun(T0, 20.0, 17.0, 17.0, T0)
    run.sample(T0 + timedelta(minutes=30), 18.0, 5.0)
    data = run.to_dict()
    assert HeatingRun.from_dict(data).to_dict() == data
    assert HeatingRun.from_dict(None) is None
