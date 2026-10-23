"""Export, import and undo of learnt values through Home Assistant (D39-D44)."""

from __future__ import annotations

import copy
import json
import logging
from datetime import timedelta
from pathlib import Path

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_capture_events

from custom_components.hearth.const import (
    CONF_BAND_MAX,
    CONF_BAND_MIN,
    CONF_BASE_TEMP,
    CONF_ROOM_NAME,
    CONF_VTHERM,
    DOMAIN,
    EVENT_DIAGNOSTIC,
)
from custom_components.hearth.core.comfort import RunningMeanState
from custom_components.hearth.core.learning_io import parse_blob
from custom_components.hearth.core.override import Ledger
from custom_components.hearth.core.preheat import HeatingRun, WarmingModel
from custom_components.hearth.diagnostics import async_get_config_entry_diagnostics

from .conftest import VTHERM, make_entry, room_store_key, seed_store, set_numbers, set_vtherm, setup_room
from .core.test_learning_io import HALF_LIFE
from .core.test_learning_io import entry as ledger_entry
from .core.test_learning_io import ledger as sample_ledger
from .core.test_learning_io import warming as sample_warming

MANIFEST_VERSION = json.loads((Path(__file__).parent.parent / "custom_components" / "hearth" / "manifest.json").read_text())["version"]

BEDROOM = "climate.bedroom"
ENTRY_ID_FIELD = "entry_id"


async def learned_room(hass: HomeAssistant, hass_storage, services, **seed):
    """A room with a warming model, a ledger and a running mean that all hold something."""
    seed_store(hass_storage, t_rm=11.0, **seed)
    set_vtherm(hass, preset="comfort", current=19.0, target=20.5)
    set_numbers(hass)
    entry = make_entry()
    room = await setup_room(hass, entry)
    room.warming_model = sample_warming()
    room.ledger = sample_ledger()
    return room, entry


async def call(hass: HomeAssistant, service: str, data: dict | None = None, *, response: bool = False):
    return await hass.services.async_call(
        DOMAIN, service, {"vtherm_entity_id": VTHERM, **(data or {})}, blocking=True, return_response=response
    )


async def export(hass: HomeAssistant, parts: list[str] | None = None) -> dict:
    return await call(hass, "export_learning", {"parts": parts} if parts else None, response=True)


def other_room_blob(hass: HomeAssistant) -> dict:
    """A blob as another room might have exported it: different numbers, different VTherm."""
    model = WarmingModel(max_runs=30)
    for delta, rate in ((8.0, 2.4), (12.0, 2.1), (16.0, 1.9)):
        model.add_run(delta, rate)
    other = Ledger()
    other.add(ledger_entry(15, "slope_error", -1, 0.75), HALF_LIFE)
    rm = RunningMeanState(
        t_rm=7.25, t_rm_day="2026-01-09", seeded_at=(dt_util.utcnow() - timedelta(days=9)).isoformat(), history=[7.0, 7.5]
    )
    return {
        "format": "hearth.learning",
        "version": 1,
        "exported_at": "2026-01-10T07:00:00+00:00",
        "hearth_version": "0.7.0",
        "room": "Bedroom",
        "vtherm": BEDROOM,
        "parts": {"warming_rate": model.to_dict(), "ledger": other.to_dict(), "running_mean": rm.to_dict()},
    }


# ---------------------------------------------------------------- export


async def test_export_single_room_returns_the_bare_blob(hass: HomeAssistant, hass_storage, services) -> None:
    room, _ = await learned_room(hass, hass_storage, services)
    blob = await export(hass)
    assert blob["format"] == "hearth.learning" and blob["version"] == 1
    assert blob["room"] == "Living Room" and blob["vtherm"] == VTHERM
    assert blob["hearth_version"] == MANIFEST_VERSION
    assert dt_util.parse_datetime(blob["exported_at"]) is not None
    assert set(blob["parts"]) == {"warming_rate", "ledger", "running_mean"}
    assert blob["parts"]["warming_rate"] == room.warming_model.to_dict()
    assert blob["parts"]["ledger"] == room.ledger.to_dict()
    assert blob["parts"]["running_mean"] == room.running_mean.to_dict()
    assert "rooms" not in blob


async def test_export_by_entry_id_is_also_bare(hass: HomeAssistant, hass_storage, services) -> None:
    _, entry = await learned_room(hass, hass_storage, services)
    blob = await hass.services.async_call(DOMAIN, "export_learning", {"entry_id": entry.entry_id}, blocking=True, return_response=True)
    assert blob["vtherm"] == VTHERM and "parts" in blob


async def test_export_parts_and_empty_parts_are_left_out(hass: HomeAssistant, hass_storage, services) -> None:
    room, _ = await learned_room(hass, hass_storage, services)
    assert set((await export(hass, ["ledger"]))["parts"]) == {"ledger"}
    assert set((await export(hass, ["warming_rate", "running_mean"]))["parts"]) == {"warming_rate", "running_mean"}
    room.warming_model = WarmingModel()
    assert set((await export(hass))["parts"]) == {"ledger", "running_mean"}
    assert (await export(hass, ["warming_rate"]))["parts"] == {}


async def test_export_unknown_part_is_refused(hass: HomeAssistant, hass_storage, services) -> None:
    await learned_room(hass, hass_storage, services)
    with pytest.raises(ServiceValidationError, match="Unknown learning part 'thermostat'"):
        await export(hass, ["thermostat"])


async def test_export_with_no_room_returns_every_room(hass: HomeAssistant, hass_storage, services) -> None:
    room, _ = await learned_room(hass, hass_storage, services)
    hass.states.async_set(BEDROOM, "heat", {"friendly_name": "Bedroom", "preset_mode": "eco", "preset_modes": ["none", "eco", "comfort"]})
    hass_storage["hearth.room.climate_bedroom"] = {
        "version": 1,
        "key": "hearth.room.climate_bedroom",
        "data": {
            "vtherm": BEDROOM,
            "settings": {},
            "running_mean": {"t_rm": 6.0, "seeded_at": (dt_util.utcnow() - timedelta(days=3)).isoformat()},
        },
    }
    bedroom_entry = MockConfigEntry(
        domain=DOMAIN,
        title="Hearth Bedroom",
        unique_id=BEDROOM,
        data={CONF_VTHERM: BEDROOM, CONF_ROOM_NAME: "Bedroom"},
        options={CONF_BASE_TEMP: 19.0, CONF_BAND_MIN: 17.0, CONF_BAND_MAX: 21.0},
    )
    bedroom_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(bedroom_entry.entry_id)
    await hass.async_block_till_done()
    result = await hass.services.async_call(DOMAIN, "export_learning", {}, blocking=True, return_response=True)
    assert set(result) == {"rooms"}
    assert set(result["rooms"]) == {VTHERM, BEDROOM}
    assert result["rooms"][VTHERM]["format"] == "hearth.learning"
    assert set(result["rooms"][VTHERM]["parts"]) == {"warming_rate", "ledger", "running_mean"}
    assert set(result["rooms"][BEDROOM]["parts"]) == {"running_mean"}
    assert result["rooms"][BEDROOM]["room"] == "Bedroom"
    # With one room installed and none named the shape is the same: it depends on the call, not on how many rooms exist
    assert room.entry_id in hass.data[DOMAIN]["rooms"]


async def test_export_does_not_change_the_room(hass: HomeAssistant, hass_storage, services) -> None:
    room, _ = await learned_room(hass, hass_storage, services)
    before = (room.warming_model.to_dict(), room.ledger.to_dict(), room.running_mean.to_dict())
    blob = await export(hass)
    blob["parts"]["warming_rate"]["runs"].clear()  # editing the response must not reach the live model
    assert (room.warming_model.to_dict(), room.ledger.to_dict(), room.running_mean.to_dict()) == before


# ---------------------------------------------------------------- import


async def test_import_replaces_the_part_and_leaves_the_others(hass: HomeAssistant, hass_storage, services) -> None:
    room, _ = await learned_room(hass, hass_storage, services)
    ledger_before = room.ledger.to_dict()
    running_before = room.running_mean.to_dict()
    blob = other_room_blob(hass)
    await call(hass, "import_learning", {"data": blob, "parts": ["warming_rate"]})
    assert room.warming_model.to_dict() == {**blob["parts"]["warming_rate"], "max_runs": int(room.const("max_learning_runs"))}
    assert room.ledger.to_dict() == ledger_before
    assert room.running_mean.to_dict() == running_before


async def test_import_default_takes_every_part_in_the_blob(hass: HomeAssistant, hass_storage, services) -> None:
    room, _ = await learned_room(hass, hass_storage, services)
    blob = other_room_blob(hass)
    await call(hass, "import_learning", {"data": blob})
    assert room.warming_model.runs == [(8.0, 2.4), (12.0, 2.1), (16.0, 1.9)]
    assert room.ledger.to_dict() == blob["parts"]["ledger"]
    assert len(room.ledger.entries) == 1
    assert room.running_mean.to_dict() == blob["parts"]["running_mean"]
    assert room.tracker.state is room.running_mean
    assert hass.states.get("sensor.hearth_living_room_running_mean_outdoor").state == "7.25"
    assert float(hass.states.get("sensor.hearth_living_room_warming_rate").attributes["evidence_count"]) == 3


async def test_import_saves_immediately_and_recomputes(hass: HomeAssistant, hass_storage, services, freezer) -> None:
    room, _ = await learned_room(hass, hass_storage, services)
    evaluated = room.last_evaluated
    freezer.tick(timedelta(minutes=1))
    await call(hass, "import_learning", {"data": other_room_blob(hass), "parts": ["warming_rate", "running_mean"]})
    data = hass_storage[room_store_key()]["data"]  # no 30 s wait: the store already holds the import
    assert data["mechanisms"]["warming"]["runs"][0] == [8.0, 2.4]
    assert data["running_mean"]["t_rm"] == 7.25
    assert room.last_evaluated > evaluated


async def test_import_warming_drops_the_run_in_progress(hass: HomeAssistant, hass_storage, services) -> None:
    room, _ = await learned_room(hass, hass_storage, services)
    now = dt_util.utcnow()
    room.heating_run = HeatingRun(now, 20.5, 17.0, 17.0, now)
    await call(hass, "import_learning", {"data": other_room_blob(hass), "parts": ["warming_rate"]})
    assert room.heating_run is None and room.preheat_plan is None
    # a ledger-only import leaves a run in progress alone (checked on the apply step: the evaluate that
    # follows an import would end an idle test run for its own reasons)
    room.heating_run = HeatingRun(now, 20.5, 17.0, 17.0, now)
    room._apply_learning(parse_blob(other_room_blob(hass), ["ledger"]))
    assert room.heating_run is not None


async def test_import_running_mean_starts_no_new_seed_hold(hass: HomeAssistant, hass_storage, services) -> None:
    room, _ = await learned_room(hass, hass_storage, services, seeded_days_ago=0.1)
    assert room.seed_hold_active  # seeded two and a half hours ago: the 48 h hold is running
    blob = other_room_blob(hass)
    await call(hass, "import_learning", {"data": blob, "parts": ["running_mean"]})
    assert room.running_mean.seeded_at == blob["parts"]["running_mean"]["seeded_at"]
    assert not room.seed_hold_active  # the imported state is nine days old, so the hold is over


async def test_import_vtherm_mismatch_warns_but_applies(hass: HomeAssistant, hass_storage, services, caplog) -> None:
    room, _ = await learned_room(hass, hass_storage, services)
    events = async_capture_events(hass, EVENT_DIAGNOSTIC)
    with caplog.at_level(logging.WARNING, logger="custom_components.hearth"):
        await call(hass, "import_learning", {"data": other_room_blob(hass), "parts": ["warming_rate", "ledger"]})
    assert any("different VTherm" in r.message and BEDROOM in r.message and r.levelno == logging.WARNING for r in caplog.records)
    assert room.warming_model.n_runs == 3 and room.warming_model.runs[0] == (8.0, 2.4)
    await hass.async_block_till_done()
    imported = [e.data for e in events if e.data["kind"] == "import_learning"]
    assert len(imported) == 1
    assert "warming_rate, ledger" in imported[0]["message"] and BEDROOM in imported[0]["message"]


async def test_import_from_the_same_vtherm_does_not_warn(hass: HomeAssistant, hass_storage, services, caplog) -> None:
    await learned_room(hass, hass_storage, services)
    blob = await export(hass)
    with caplog.at_level(logging.WARNING, logger="custom_components.hearth"):
        await call(hass, "import_learning", {"data": blob})
    assert not [r for r in caplog.records if "different VTherm" in r.message]


async def test_import_export_round_trip_is_exact(hass: HomeAssistant, hass_storage, services) -> None:
    room, _ = await learned_room(hass, hass_storage, services)
    blob = await export(hass)
    expected = (room.warming_model.to_dict(), room.ledger.to_dict(), room.running_mean.to_dict())
    room.warming_model = WarmingModel()
    room.ledger = Ledger()
    room.running_mean = RunningMeanState(t_rm=1.0, seeded_at=dt_util.utcnow().isoformat())
    await call(hass, "import_learning", {"data": blob})
    assert (room.warming_model.to_dict(), room.ledger.to_dict(), room.running_mean.to_dict()) == expected


async def test_import_validates_before_applying_anything(hass: HomeAssistant, hass_storage, services) -> None:
    room, _ = await learned_room(hass, hass_storage, services)
    before = (room.warming_model.to_dict(), room.ledger.to_dict(), room.running_mean.to_dict())
    blob = other_room_blob(hass)
    blob["parts"]["ledger"]["entries"][0]["bucket"] = "made_up"  # warming_rate is first and fine; ledger is second and bad
    with pytest.raises(ServiceValidationError, match="entries.0..bucket"):
        await call(hass, "import_learning", {"data": blob})
    assert (room.warming_model.to_dict(), room.ledger.to_dict(), room.running_mean.to_dict()) == before
    blob = other_room_blob(hass)
    blob["parts"]["running_mean"]["t_rm"] = "cold"  # the last part is bad
    with pytest.raises(ServiceValidationError, match="running_mean.t_rm"):
        await call(hass, "import_learning", {"data": blob})
    assert (room.warming_model.to_dict(), room.ledger.to_dict(), room.running_mean.to_dict()) == before
    assert hass_storage[room_store_key()]["data"]["mechanisms"].get("warming", {}).get("runs", [])[:1] != [[8.0, 2.4]]


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda b: b.update(format="other"), "format must be"),
        (lambda b: b.update(version=2), "Unsupported learning data version 2"),
        (lambda b: b["parts"].update(boiler={}), "Unknown part 'boiler'"),
        (lambda b: b["parts"]["warming_rate"].update(runs=[[1.0]]), "must be a .delta, rate. pair"),
    ],
)
async def test_import_refuses_bad_data(hass: HomeAssistant, hass_storage, services, mutate, message) -> None:
    room, _ = await learned_room(hass, hass_storage, services)
    before = room.warming_model.to_dict()
    blob = other_room_blob(hass)
    mutate(blob)
    with pytest.raises(ServiceValidationError, match=message):
        await call(hass, "import_learning", {"data": blob})
    assert room.warming_model.to_dict() == before


async def test_import_refuses_unknown_parts_missing_parts_and_no_room(hass: HomeAssistant, hass_storage, services) -> None:
    await learned_room(hass, hass_storage, services)
    blob = other_room_blob(hass)
    with pytest.raises(ServiceValidationError, match="Unknown learning part 'boiler'"):
        await call(hass, "import_learning", {"data": blob, "parts": ["boiler"]})
    del blob["parts"]["ledger"]
    with pytest.raises(ServiceValidationError, match="no 'ledger' part"):
        await call(hass, "import_learning", {"data": blob, "parts": ["ledger"]})
    with pytest.raises(ServiceValidationError, match="Specify entry_id or vtherm_entity_id"):
        await hass.services.async_call(DOMAIN, "import_learning", {"data": blob}, blocking=True)


async def test_import_accepts_diagnostics_parts_wrapped_in_a_blob(hass: HomeAssistant, hass_storage, services) -> None:
    room, entry = await learned_room(hass, hass_storage, services)
    diag = (await async_get_config_entry_diagnostics(hass, entry))["room"]
    blob = {
        "format": "hearth.learning",
        "version": 1,
        "parts": {"warming_rate": diag["mechanisms"]["warming"], "ledger": diag["ledger"], "running_mean": diag["running_mean"]},
    }
    expected = (room.warming_model.to_dict(), room.ledger.to_dict(), room.running_mean.to_dict())
    room.warming_model = WarmingModel()
    room.ledger = Ledger()
    await call(hass, "import_learning", {"data": blob})
    assert (room.warming_model.to_dict(), room.ledger.to_dict(), room.running_mean.to_dict()) == expected


# ---------------------------------------------------------------- reset and undo


async def test_reset_then_undo_restores_warming_and_ledger_exactly(hass: HomeAssistant, hass_storage, services) -> None:
    room, _ = await learned_room(hass, hass_storage, services)
    warming_before = room.warming_model.to_dict()
    ledger_before = room.ledger.to_dict()
    await call(hass, "reset_learning", {"bucket": "warming_rate"})
    assert room.warming_model.n_runs == 0
    assert room.last_reset["bucket"] == "warming_rate" and set(room.last_reset["blob"]["parts"]) == {"warming_rate"}
    await call(hass, "undo_reset")
    assert room.warming_model.to_dict() == warming_before
    assert room.last_reset is None
    await call(hass, "reset_learning")  # every ledger bucket
    assert room.ledger.entries == []
    assert room.last_reset["bucket"] is None and set(room.last_reset["blob"]["parts"]) == {"ledger"}
    await call(hass, "undo_reset")
    assert room.ledger.to_dict() == ledger_before
    assert room.warming_model.to_dict() == warming_before


async def test_single_bucket_undo_restores_only_that_bucket(hass: HomeAssistant, hass_storage, services) -> None:
    room, _ = await learned_room(hass, hass_storage, services)
    before = room.ledger.to_dict()
    await call(hass, "reset_learning", {"bucket": "baseline_error"})
    assert not [e for e in room.ledger.entries if e.bucket == "baseline_error"]
    # The stash is the whole ledger as it was, so it is still a valid export
    assert room.last_reset["blob"]["parts"]["ledger"] == before
    # Something is learnt in another bucket before the undo: the undo must not wipe it
    room.ledger.add(ledger_entry(500, "skip_failure", -1, 0.25), HALF_LIFE)
    skip_after_reset = room.ledger.buckets["skip_failure"].to_dict()
    await call(hass, "undo_reset")
    restored = room.ledger
    assert restored.buckets["baseline_error"].to_dict() == before["buckets"]["baseline_error"]
    assert [e.bucket for e in restored.entries].count("baseline_error") == 2
    assert restored.buckets["skip_failure"].to_dict() == skip_after_reset
    assert [e.bucket for e in restored.entries].count("skip_failure") == 2


async def test_undo_with_nothing_stashed_raises(hass: HomeAssistant, hass_storage, services) -> None:
    await learned_room(hass, hass_storage, services)
    with pytest.raises(ServiceValidationError, match="no reset to undo"):
        await call(hass, "undo_reset")
    with pytest.raises(ServiceValidationError, match="Specify entry_id or vtherm_entity_id"):
        await hass.services.async_call(DOMAIN, "undo_reset", {}, blocking=True)


async def test_a_second_undo_has_nothing_to_undo(hass: HomeAssistant, hass_storage, services) -> None:
    room, _ = await learned_room(hass, hass_storage, services)
    await call(hass, "reset_learning", {"bucket": "warming_rate"})
    await call(hass, "undo_reset")
    with pytest.raises(ServiceValidationError, match="no reset to undo"):
        await call(hass, "undo_reset")
    assert room.warming_model.n_runs == 3


async def test_only_the_latest_reset_is_kept(hass: HomeAssistant, hass_storage, services) -> None:
    room, _ = await learned_room(hass, hass_storage, services)
    await call(hass, "reset_learning", {"bucket": "warming_rate"})
    await call(hass, "reset_learning", {"bucket": "skip_failure"})
    assert room.last_reset["bucket"] == "skip_failure"
    await call(hass, "undo_reset")
    assert room.warming_model.n_runs == 0  # the earlier warming-rate reset can no longer be undone
    assert [e.bucket for e in room.ledger.entries].count("skip_failure") == 1


async def test_a_reset_that_clears_nothing_keeps_the_earlier_stash(hass: HomeAssistant, hass_storage, services) -> None:
    room, _ = await learned_room(hass, hass_storage, services)
    await call(hass, "reset_learning", {"bucket": "warming_rate"})
    stash = copy.deepcopy(room.last_reset)
    await call(hass, "reset_learning", {"bucket": "warming_rate"})  # already empty
    assert room.last_reset == stash
    room.ledger = Ledger()
    await call(hass, "reset_learning")  # empty ledger
    assert room.last_reset == stash
    await call(hass, "undo_reset")
    assert room.warming_model.n_runs == 3


async def test_undo_keeps_runs_learnt_since_the_reset(hass: HomeAssistant, hass_storage, services) -> None:
    room, _ = await learned_room(hass, hass_storage, services)
    await call(hass, "reset_learning", {"bucket": "warming_rate"})
    room.warming_model.add_run(12.0, 1.6)
    await call(hass, "undo_reset")
    assert room.warming_model.runs == [*sample_warming().runs, (12.0, 1.6)]


async def test_reset_stash_is_saved_at_once_and_survives_a_reload(hass: HomeAssistant, hass_storage, services) -> None:
    room, entry = await learned_room(hass, hass_storage, services)
    warming_before = room.warming_model.to_dict()
    await call(hass, "reset_learning", {"bucket": "warming_rate"})
    stored = hass_storage[room_store_key()]["data"]["last_reset"]  # already there: no waiting for the 30 s save
    assert stored["bucket"] == "warming_rate" and stored["blob"]["parts"]["warming_rate"] == warming_before
    assert hass_storage[room_store_key()]["data"]["mechanisms"]["warming"]["runs"] == []
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    reloaded = hass.data[DOMAIN]["rooms"][entry.entry_id]
    assert reloaded is not room
    assert reloaded.last_reset == stored
    assert reloaded.warming_model.n_runs == 0
    await call(hass, "undo_reset")
    assert reloaded.warming_model.to_dict() == warming_before
    assert reloaded.last_reset is None
    assert hass_storage[room_store_key()]["data"]["last_reset"] is None


async def test_a_store_from_0_6_without_last_reset_loads(hass: HomeAssistant, hass_storage, services) -> None:
    seed_store(hass_storage, t_rm=11.0, extra={"mechanisms": {"warming": sample_warming().to_dict()}})
    assert "last_reset" not in hass_storage[room_store_key()]["data"]
    set_vtherm(hass)
    set_numbers(hass)
    room = await setup_room(hass, make_entry())
    assert room.last_reset is None
    assert room.warming_model.n_runs == 3
    assert hass.states.get("sensor.hearth_living_room_warming_rate").attributes["last_reset_at"] is None
    assert room.diagnostics()["last_reset"] is None


@pytest.mark.parametrize(
    "damaged", ["yesterday", {"at": 5, "blob": {}}, {"at": "2026-01-01T00:00:00+00:00", "bucket": None, "blob": {"format": "no"}}]
)
async def test_a_damaged_stash_is_dropped_not_fatal(hass: HomeAssistant, hass_storage, services, damaged) -> None:
    seed_store(hass_storage, t_rm=11.0, extra={"last_reset": damaged})
    set_vtherm(hass)
    set_numbers(hass)
    room = await setup_room(hass, make_entry())
    assert room.last_reset is None
    with pytest.raises(ServiceValidationError, match="no reset to undo"):
        await call(hass, "undo_reset")


async def test_last_reset_is_visible_on_sensors_and_in_diagnostics(hass: HomeAssistant, hass_storage, services) -> None:
    room, entry = await learned_room(hass, hass_storage, services)
    warming = hass.states.get("sensor.hearth_living_room_warming_rate")
    assert warming.attributes["last_reset_at"] is None and warming.attributes["last_reset_bucket"] is None
    await call(hass, "reset_learning", {"bucket": "baseline_error"})
    at = room.last_reset["at"]
    for entity in ("sensor.hearth_living_room_warming_rate", "sensor.hearth_living_room_learning_baseline_error"):
        attrs = hass.states.get(entity).attributes
        assert attrs["last_reset_at"] == at and attrs["last_reset_bucket"] == "baseline_error"
        assert "blob" not in attrs and "last_reset" not in attrs
    diag = await async_get_config_entry_diagnostics(hass, entry)
    assert diag["room"]["last_reset"]["bucket"] == "baseline_error"
    assert diag["room"]["last_reset"]["at"] == at
    assert diag["room"]["last_reset"]["blob"]["format"] == "hearth.learning"
    await call(hass, "undo_reset")
    attrs = hass.states.get("sensor.hearth_living_room_warming_rate").attributes
    assert attrs["last_reset_at"] is None and attrs["last_reset_bucket"] is None
    assert (await async_get_config_entry_diagnostics(hass, entry))["room"]["last_reset"] is None


async def test_undo_emits_a_diagnostic_event(hass: HomeAssistant, hass_storage, services) -> None:
    await learned_room(hass, hass_storage, services)
    events = async_capture_events(hass, EVENT_DIAGNOSTIC)
    await call(hass, "reset_learning", {"bucket": "warming_rate"})
    await call(hass, "undo_reset")
    await hass.async_block_till_done()
    kinds = [e.data["kind"] for e in events]
    assert "reset_learning" in kinds and "undo_reset" in kinds
    undo = next(e.data for e in events if e.data["kind"] == "undo_reset")
    assert "warming rate" in undo["message"] and "warming_rate" in undo["message"]
