from datetime import UTC, datetime, timedelta

from custom_components.hearth.core.boost import NON_WORKDAY, WORKDAY, BoostEvent, prune, suggestions

NOW = datetime(2026, 10, 30, 15, 0, tzinfo=UTC)


def ev(days_ago: float, day_type: str = WORKDAY, hour: int = 14, preset: str = "eco") -> BoostEvent:
    return BoostEvent(NOW - timedelta(days=days_ago), day_type, hour, preset)


def test_three_boosts_in_the_same_slot_make_a_suggestion():
    out = suggestions([ev(1), ev(2), ev(8)], NOW, 28, 3)
    assert len(out) == 1
    assert out[0]["slot"] == "14:00-15:00" and out[0]["count"] == 3 and out[0]["day_type"] == WORKDAY
    assert out[0]["presets"] == ["eco"]


def test_two_boosts_or_mixed_slots_do_not():
    assert suggestions([ev(1), ev(2)], NOW, 28, 3) == []
    assert suggestions([ev(1), ev(2, hour=15), ev(3, day_type=NON_WORKDAY)], NOW, 28, 3) == []


def test_old_boosts_fall_out_of_the_window():
    events = [ev(1), ev(2), ev(40)]
    assert suggestions(events, NOW, 28, 3) == []
    assert len(prune(events, NOW, 28)) == 2


def test_round_trip_and_ordering():
    e = ev(1)
    assert BoostEvent.from_dict(e.to_dict()) == e
    events = [ev(d, hour=9) for d in (1, 2, 3, 4)] + [ev(d) for d in (1, 2, 3)]
    out = suggestions(events, NOW, 28, 3)
    assert [s["hour"] for s in out] == [9, 14]
    assert suggestions([ev(1, hour=23), ev(2, hour=23), ev(3, hour=23)], NOW, 28, 3)[0]["slot"] == "23:00-00:00"
