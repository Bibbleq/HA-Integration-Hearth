"""Tests for core.scheduler_source: deriving a Hearth schedule from Scheduler component attributes."""

from datetime import datetime, time
from zoneinfo import ZoneInfo

import pytest

from custom_components.hearth.core.schedule import NON_WORKDAY, Block, Schedule
from custom_components.hearth.core.scheduler_source import (
    ISSUE_DAY_CONFLICT,
    ISSUE_UNSUPPORTED_ACTION,
    SourceError,
    SourceInput,
    action_preset,
    derive_schedule,
    entity_blocks,
    expand_weekdays,
    mark_skippable,
    slot_start,
)

WARM = ("comfort", "boost")


def act(preset: str) -> dict:
    return {"service": "climate.set_preset_mode", "data": {"preset_mode": preset}}


# The live "Living Room" pair from the real system.
WEEKDAY_ENTITY = SourceInput(
    "switch.schedule_living_room_weekday",
    ["mon", "tue", "wed", "thu", "fri"],
    ["00:00:00 - 06:30:00", "06:30:00 - 09:00:00", "09:00:00 - 16:00:00", "16:00:00 - 22:00:00", "22:00:00 - 00:00:00"],
    [act("eco"), act("comfort"), act("eco"), act("comfort"), act("eco")],
)
WEEKEND_ENTITY = SourceInput(
    "switch.schedule_living_room_weekend",
    ["sat", "sun"],
    ["00:00:00 - 06:45:00", "06:45:00 - 22:00:00", "22:00:00 - 00:00:00"],
    [act("eco"), act("comfort"), act("eco")],
)


def derive(*sources, warm=WARM, skippable=True, cutoff=time(12, 0)):
    return derive_schedule(sources, warm_by_presets=warm, skippable_first_morning=skippable, morning_cutoff=cutoff)


# ------------------------------------------------------------------ weekdays


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (["daily"], {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}),
        (["workday"], {"mon", "tue", "wed", "thu", "fri"}),
        (["weekend"], {"sat", "sun"}),
        (["mon", "wed"], {"mon", "wed"}),
        (["Monday", "SAT"], {"mon", "sat"}),
        (["workday", "sat"], {"mon", "tue", "wed", "thu", "fri", "sat"}),
        (["mon", "mon", "daily"], {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}),
    ],
)
def test_expand_weekdays(raw, expected):
    assert expand_weekdays(raw) == expected


@pytest.mark.parametrize("raw", [None, [], "mon", ["funday"], [3]])
def test_expand_weekdays_rejects(raw):
    with pytest.raises(SourceError):
        expand_weekdays(raw)


# ------------------------------------------------------------------ slots and actions


@pytest.mark.parametrize(
    ("slot", "expected"),
    [
        ("06:30:00 - 09:00:00", time(6, 30)),
        ("06:30:00", time(6, 30)),
        ("22:00:00 - 00:00:00", time(22, 0)),
        ("00:00:00 - 00:00:00", time(0, 0)),  # whole-day wrap: one block at midnight
        ("6:05", time(6, 5)),
        ("23:59:59 - 00:00:00", time(23, 59)),
    ],
)
def test_slot_start(slot, expected):
    assert slot_start(slot) == expected


@pytest.mark.parametrize("slot", ["sunrise - 09:00:00", "sunset+00:30:00", "25:00:00", "12:61:00", "", None, 630])
def test_slot_start_rejects(slot):
    with pytest.raises(SourceError):
        slot_start(slot)


def test_action_preset():
    assert action_preset(act("Eco")) == "eco"
    assert action_preset({"service": "climate.set_preset_mode", "service_data": {"preset_mode": "boost"}}) == "boost"


@pytest.mark.parametrize(
    "action",
    [
        {"service": "climate.set_temperature", "data": {"temperature": 20}},
        {"service": "climate.set_hvac_mode", "data": {"hvac_mode": "heat"}},
        {"service": "climate.set_preset_mode"},
        {"service": "climate.set_preset_mode", "data": {"preset_mode": "away"}},
        {"service": "climate.set_preset_mode", "data": {"preset_mode": "none"}},
        {"service": "light.turn_on"},
        {"data": {"preset_mode": "eco"}},
        "climate.set_preset_mode",
        None,
    ],
)
def test_action_preset_rejects(action):
    with pytest.raises(SourceError):
        action_preset(action)


def test_slot_without_stop_time():
    source = SourceInput("switch.s", ["daily"], ["06:00:00", "20:00:00"], [act("comfort"), act("eco")])
    blocks = entity_blocks(source, WARM)
    assert blocks == [Block(time(6, 0), "comfort", True), Block(time(20, 0), "eco", False)]


def test_slot_action_alignment_survives_unsorted_input():
    source = SourceInput("switch.s", ["daily"], ["22:00:00 - 00:00:00", "06:30:00 - 09:00:00"], [act("eco"), act("comfort")])
    blocks = entity_blocks(source, WARM)
    assert [(b.at, b.preset) for b in blocks] == [(time(6, 30), "comfort"), (time(22, 0), "eco")]


def test_whole_day_wrap_slot_is_one_block():
    source = SourceInput("switch.s", ["daily"], ["00:00:00 - 00:00:00"], [act("comfort")])
    assert entity_blocks(source, WARM) == [Block(time(0, 0), "comfort", True)]


@pytest.mark.parametrize(
    "source",
    [
        SourceInput("s", ["daily"], ["06:00:00", "07:00:00"], [act("eco")]),  # misaligned
        SourceInput("s", ["daily"], [], []),
        SourceInput("s", ["daily"], None, None),
        SourceInput("s", ["daily"], ["06:00:00", "06:00:00 - 07:00:00"], [act("eco"), act("comfort")]),  # same start
    ],
)
def test_entity_blocks_rejects(source):
    with pytest.raises(SourceError):
        entity_blocks(source, WARM)


# ------------------------------------------------------------------ warm-by and skippable


def test_warm_by_follows_presets():
    source = SourceInput(
        "s", ["daily"], ["05:00:00", "06:00:00", "07:00:00", "08:00:00"], [act("frost"), act("eco"), act("comfort"), act("boost")]
    )
    kinds = {b.preset: b.warm_by for b in entity_blocks(source, WARM)}
    assert kinds == {"frost": False, "eco": False, "comfort": True, "boost": True}
    kinds = {b.preset: b.warm_by for b in entity_blocks(source, ("eco",))}
    assert kinds == {"frost": False, "eco": True, "comfort": False, "boost": False}
    assert not any(b.warm_by for b in entity_blocks(source, ()))


def test_mark_skippable_first_warm_by_before_cutoff_only():
    blocks = [Block(time(6, 30), "comfort", True), Block(time(9, 0), "eco", False), Block(time(17, 0), "comfort", True)]
    marked = mark_skippable(blocks, time(12, 0))
    assert [b.skippable for b in marked] == [True, False, False]


def test_mark_skippable_none_when_first_warm_by_is_after_cutoff():
    blocks = [Block(time(0, 0), "eco", False), Block(time(13, 0), "comfort", True), Block(time(18, 0), "comfort", True)]
    assert not any(b.skippable for b in mark_skippable(blocks, time(12, 0)))


def test_mark_skippable_respects_cutoff_value():
    blocks = [Block(time(10, 0), "comfort", True)]
    assert mark_skippable(blocks, time(9, 0))[0].skippable is False
    assert mark_skippable(blocks, time(10, 30))[0].skippable is True


# ------------------------------------------------------------------ derive_schedule


def test_live_pair_derives_weekdays_weekend_and_non_workday():
    result = derive(WEEKDAY_ENTITY, WEEKEND_ENTITY)
    assert result.ok and result.issues == []
    blob = result.schedule.to_dict()
    weekday = [
        {"at": "00:00", "preset": "eco"},
        {"warm_by": "06:30", "preset": "comfort", "skippable": True},
        {"at": "09:00", "preset": "eco"},
        {"warm_by": "16:00", "preset": "comfort"},
        {"at": "22:00", "preset": "eco"},
    ]
    weekend = [
        {"at": "00:00", "preset": "eco"},
        {"warm_by": "06:45", "preset": "comfort", "skippable": True},
        {"at": "22:00", "preset": "eco"},
    ]
    for day in ("mon", "tue", "wed", "thu", "fri"):
        assert blob[day] == weekday
    for day in ("sat", "sun", NON_WORKDAY):
        assert blob[day] == weekend


def test_derived_blob_round_trips_through_schedule_parse():
    result = derive(WEEKDAY_ENTITY, WEEKEND_ENTITY)
    reparsed = Schedule.parse(result.schedule.to_dict())
    assert reparsed.to_dict() == result.schedule.to_dict()
    assert not reparsed.is_empty


def test_non_workday_only_from_an_exact_weekend_entity():
    saturday_only = SourceInput("switch.sat", ["sat"], ["08:00:00"], [act("comfort")])
    sunday_only = SourceInput("switch.sun", ["sun"], ["09:00:00"], [act("comfort")])
    result = derive(WEEKDAY_ENTITY, saturday_only, sunday_only)
    assert result.ok
    assert NON_WORKDAY not in result.schedule.to_dict()  # the existing fallback (Saturday) applies
    shorthand = SourceInput("switch.we", ["weekend"], ["08:00:00"], [act("comfort")])
    assert NON_WORKDAY in derive(WEEKDAY_ENTITY, shorthand).schedule.to_dict()
    extra = SourceInput("switch.we", ["sat", "sun", "fri"], ["08:00:00"], [act("comfort")])
    assert NON_WORKDAY not in derive(extra).schedule.to_dict()


def test_workday_and_weekend_shorthands_cover_the_week():
    workday = SourceInput("switch.wd", ["workday"], ["06:00:00"], [act("comfort")])
    weekend = SourceInput("switch.we", ["weekend"], ["08:00:00"], [act("comfort")])
    blob = derive(workday, weekend).schedule.to_dict()
    assert set(blob) == {"mon", "tue", "wed", "thu", "fri", "sat", "sun", NON_WORKDAY}


def test_uncovered_days_have_no_blocks():
    result = derive(WEEKDAY_ENTITY)
    blob = result.schedule.to_dict()
    assert "sat" not in blob and "sun" not in blob and NON_WORKDAY not in blob
    assert result.ok


def test_skippable_can_be_switched_off():
    blob = derive(WEEKDAY_ENTITY, skippable=False).schedule.to_dict()
    assert not any(b.get("skippable") for b in blob["mon"])


def test_no_warm_by_presets_means_all_at_blocks():
    blob = derive(WEEKDAY_ENTITY, warm=()).schedule.to_dict()
    assert all("at" in b for b in blob["mon"])
    assert not any(b.get("skippable") for b in blob["mon"])


def test_day_conflict_is_reported_and_not_applied():
    daily = SourceInput("switch.daily", ["daily"], ["07:00:00"], [act("comfort")])
    result = derive(WEEKDAY_ENTITY, daily)
    assert not result.ok
    issue = result.issue(ISSUE_DAY_CONFLICT)
    assert issue is not None
    assert set(issue.entity_ids) == {WEEKDAY_ENTITY.entity_id, "switch.daily"}
    assert "mon" in issue.detail and "sat" not in issue.detail
    blob = result.schedule.to_dict()
    assert "mon" not in blob  # conflicted days are left out
    assert "sat" in blob  # the unconflicted days are still derived


def test_overlapping_shorthand_conflicts():
    a = SourceInput("switch.a", ["workday"], ["06:00:00"], [act("comfort")])
    b = SourceInput("switch.b", ["fri", "sat"], ["07:00:00"], [act("comfort")])
    result = derive(a, b)
    assert result.issue(ISSUE_DAY_CONFLICT).detail == "days: fri"


def test_same_entity_listing_a_day_twice_is_not_a_conflict():
    source = SourceInput("switch.a", ["mon", "daily"], ["06:00:00"], [act("comfort")])
    assert derive(source).ok


def test_unsupported_action_makes_entity_unusable():
    bad = SourceInput(
        "switch.bad",
        ["sat", "sun"],
        ["06:00:00 - 22:00:00", "22:00:00 - 00:00:00"],
        [{"service": "climate.set_temperature", "data": {"temperature": 21}}, act("eco")],
    )
    result = derive(WEEKDAY_ENTITY, bad)
    assert not result.ok
    issue = result.issue(ISSUE_UNSUPPORTED_ACTION)
    assert issue.entity_ids == ("switch.bad",)
    assert "climate.set_temperature" in issue.detail
    assert "sat" not in result.schedule.to_dict()


def test_unsupported_and_conflict_can_both_be_reported():
    bad = SourceInput("switch.bad", ["mon"], ["06:00:00"], [{"service": "light.turn_on"}])
    dup_a = SourceInput("switch.a", ["tue"], ["06:00:00"], [act("comfort")])
    dup_b = SourceInput("switch.b", ["tue"], ["07:00:00"], [act("comfort")])
    result = derive(bad, dup_a, dup_b)
    assert {i.key for i in result.issues} == {ISSUE_UNSUPPORTED_ACTION, ISSUE_DAY_CONFLICT}


def test_sunrise_slot_is_unsupported():
    source = SourceInput("switch.sun", ["daily"], ["sunrise - 09:00:00"], [act("comfort")])
    result = derive(source)
    assert result.issue(ISSUE_UNSUPPORTED_ACTION) is not None


def test_whole_day_wrap_derives_one_block_per_day():
    source = SourceInput("switch.s", ["daily"], ["00:00:00 - 00:00:00"], [act("comfort")])
    blob = derive(source).schedule.to_dict()
    assert blob["mon"] == [{"warm_by": "00:00", "preset": "comfort", "skippable": True}]


def test_eco_only_entity_has_no_skippable_block():
    source = SourceInput("switch.s", ["daily"], ["00:00:00 - 00:00:00"], [act("eco")])
    blob = derive(source).schedule.to_dict()
    assert blob["mon"] == [{"at": "00:00", "preset": "eco"}]


def test_schedule_current_block_carries_over_uncovered_day():
    """A day with no blocks keeps the previous day's last preset (existing model behaviour)."""
    london = ZoneInfo("Europe/London")
    schedule = derive(WEEKDAY_ENTITY).schedule  # Mon-Fri only
    saturday_noon = datetime(2026, 10, 24, 12, 0, tzinfo=london)  # a Saturday
    current = schedule.current(saturday_noon, london)
    assert current is not None and current.preset == "eco" and current.block.at == time(22, 0)
