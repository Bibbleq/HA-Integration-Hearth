# Hearth - build report

Built 2026-09-19 from the design spec (draft 1.0, August 2026). All four phases are implemented and
pushed to `main`, each as its own commit. CI (GitHub Actions) runs pytest on Home Assistant 2026.9.3 /
Python 3.14, hassfest, and HACS validation.

| Check | State |
|---|---|
| pytest (452 tests at 0.7.3: 246 pure-core, 206 HA-level) | green on CI |
| hassfest | green |
| HACS validation | strict since the repo went public (see D18) |

## What is done

**Phase 1, adaptive comfort offset (on by default).** EN 16798 running mean from outdoor samples every
15 min, daily roll at 00:10 with restart catch-up, seeding from recorder statistics / history / current
reading with a 48 h hold, comfort target with slope, reference, max-offset cap and hard band clamp,
quantised to 0.5 C within the band, written to `number.<vtherm>_preset_comfort_temp` only when the
quantised value changes since Hearth's last write. Boost optional as a second affected preset.

**Phase 2, forecast skip and setback depth (off by default).** Shared forecast manager (hourly + daily
via `weather.get_forecasts`, on start, hourly, 15 min before decision times, persisted, 3 h staleness).
Skip decision at the configured time with indoor floor, solar discount, optional T_rm-derived threshold and
hourly mode, late-decision window, eco preset switch, per-tick abort supervision (floor, manual change,
dormant, 11:00 forecast shortfall), restore at skip end. Continuous preview sensor. Bedtime setback
decision from tomorrow's 06:00-09:00 forecast, eco raised under a ceiling of band_max and the comfort
target, restored at 09:00 unless edited.

**Phase 3, warm-by schedule and learned preheat (off by default).** Per-room weekly schedule in the config
entry, `hearth.set_schedule` with validation, block application once per instance with exact one-shot
timers, manual changes standing until the next block, restart never re-firing. Preheat plan per tick from
the learned warming rate (own computation from `current_temperature` during clean heating runs, least-squares
fit of `a - b * (T_target - T_out)`, clamped, conservative 1.0 C/h until five runs), safety factor, max
preheat cap, solar discount, early preset switch. Skip and schedule interplay (skippable blocks deferred,
non-skippable block ends a skip, restore to the schedule's preset).

**Phase 4, override detection, stand-down and ledger (off by default, record-only).** Detection of preset,
manual-setpoint and preset-temp changes not in Hearth's write log; stand-down to the next block or 180 min;
ledger with the four attribution buckets, weights, exclusions (dormant, stale forecast during skips, storms,
unaffected presets), exponentially decayed means with evidence counts; `hearth.reset_learning`;
`hearth.override`. Application behind the separate `apply_learned_corrections` switch: bounded target
correction through the same clamps, skip-threshold offset, safety-factor multiplier.

**Cross-cutting.** Global `switch.hearth_active`, per-mechanism switches, five Tier 1 numbers, Tier 2
config/options flow, Tier 3 `DEFAULTS` dict with `hearth: advanced:` YAML overrides, dormant binary
sensor with reason, `hearth_diagnostic` events, config-entry diagnostics download, README, DECISIONS.md.

## What is untested

Everything below has unit or HA-harness coverage against a simulated VTherm, but has never run against a
real Versatile Thermostat or a real weather integration:

- **VTherm attribute layout in the wild.** The adapter reads the nested `specific_states`, `window_manager`,
  `safety_manager`, `power_manager` and `presence_manager` blocks (verified in VTherm 10.4 source) with a
  root-level fallback. The simulated entity in tests uses that layout; a different VTherm release could move
  a key.
- **Preset number entity resolution** through the entity registry (tests use the naming fallback because the
  simulated numbers have no registry entries).
- **Recorder seeding** (`seed.py`): the statistics and history calls are exercised only by the fallback path
  in tests. If the outdoor sensor has no long-term statistics, the history path runs; if that fails too,
  seeding uses the current reading.
- **`weather.get_forecasts` response shapes** from real providers (Met Office, OpenWeatherMap...). The
  normaliser tolerates missing `templow`/`condition` and ISO or datetime `datetime` values.
- **VTherm's reaction to manual setpoints**: the detector assumes a manual `climate.set_temperature`
  surfaces as preset `none` plus a target change (VTherm's documented behaviour).
- **Long-running timing**: DST transitions for schedule blocks are unit-tested in the core; the
  `async_track_time_change` triggers rely on HA's local-time handling.
- **Learning application** has synthetic-ledger tests only; real ledgers take weeks to accumulate.
- Tier 3 YAML overrides are parsed and type-coerced but only the defaults are exercised end to end.

## What to verify on the live HA first

1. **Preset temperature numbers exist per VTherm.** For each room you want Hearth to drive, the VTherm must
   expose `number.<vtherm>_preset_comfort_temp` (and `_eco_temp` for setback). On the current estate preset
   temps inherit from the central configuration, so untick that per VTherm. Until then the room shows
   `binary_sensor.hearth_<room>_dormant` on with reason `no_preset_entities`.
2. **Outdoor temperature source.** Check `sensor.hearth_<room>_running_mean_outdoor` attributes:
   `outdoor_source` and `last_outdoor`. If the VTherm has no outdoor sensor configured, set the outdoor
   sensor override in the room options (this also enables recorder seeding).
3. **Seeding and the 48 h hold.** After install the comfort target sensor shows `held: true` for 48 h and
   no write happens. Confirm `sensor.hearth_<room>_comfort_target` attributes look sane (offset, band) before
   the hold ends. `hearth.recompute` with `force: true` writes immediately if you want to see a write.
4. **First real write.** Watch the VTherm comfort number and the VTherm target after the first adaptive
   write; confirm VTherm picks up the number change without switching preset, and that the change shows in
   `sensor.hearth_<room>_comfort_target` under `last_write`.
5. **Dormant reasons.** Open a window, set presence away, set VTherm central mode to frost protection: the dormant sensor should follow
   with the right reason each time. If one does not, dump the VTherm's attributes and compare with D1.
6. **Forecast cache.** Set a weather entity, call `hearth.refresh_forecast`, and check
   `sensor.hearth_<room>_skip_status` attributes `forecast_fresh` and `forecast_fetched_at`, and the
   diagnostics download for the normalised cache (hourly count, daily highs, conditions).
7. **Chattiness.** The room store is written at most every 30 s while VTherm attributes change. Watch
   `.storage/hearth.room.*` size and consider recorder excludes for the Hearth diagnostic sensors if the
   attributes are noisy in history.
8. **Before phase 3 on a room**: either set the schedule with `hearth.set_schedule` and disable that room's HA
   Scheduler entries yourself, or (0.6.0) point the room at its Scheduler entities and let Hearth switch them off
   when the schedule switch goes on (Tado is already a flat-temperature actuator with no early start, so nothing
   to change there). Then check `sensor.hearth_<room>_next_block`.
9. **HACS install path**: add the repo in HACS as a custom repository (category Integration), or copy
   `custom_components/hearth` manually.

## Changes after the first build

- **0.7.3**: three gaps found on the Living Room on 2026-10-23. Schedule blocks are applied while the room is dormant
  for an open window (D57); a VTherm moving its target back to its preset's own temperature is not an override (D58);
  a bypassed window (`window_manager.is_window_bypass`) is not dormant (D59). New ignored-override reason
  `vtherm_reapply`. Verify on the live system:
  1. With a window/door open over a block boundary, `sensor.hearth_<room>_next_block` `applied_block` moves to the new
     block and the VTherm's `preset_mode` changes while its target stays on the window temperature; when the window
     closes the room is on the new block's preset.
  2. Closing a window, or reloading a VTherm's options, leaves `sensor.hearth_<room>_stand_down` at `none` and adds a
     `vtherm_reapply` entry to `recent_ignored_overrides`.
  3. `versatile_thermostat.set_window_bypass` on with the window open: `binary_sensor.hearth_<room>_dormant` goes off.
- **0.7.2**: a restart no longer stands rooms down (D55), and unchanged entities are not rewritten (D56).
  A VTherm change out of an unavailable or initialising state (`is_ready` false), or any change without a person's
  context while Home Assistant is starting and for `startup_grace_s` (180) after it has started, is a restore: it is
  listed in `recent_ignored_overrides` with reason `startup` and Hearth puts its current block back. A person's change
  in that window still counts. Hearth entities now write state only when their state, icon or attributes change, and
  `skip_preview`'s `computed_at` moves only when the preview changes. Also fixes two tests 0.7.1 left failing on CI
  (a hard-coded version, and a run that ended on idle) and adds `tests/winsock.py` for local runs on Windows
  (`pytest -p tests.winsock`). Verify on the live system:
  1. Restart Home Assistant: no `sensor.hearth_<room>_stand_down` shows a cause afterwards; each shows a `startup`
     entry in `recent_ignored_overrides`.
  2. Change a live room's preset from the dashboard within three minutes of a restart: it still stands the room down
     with cause `manual_preset`.
  3. Watch a `sensor.hearth_<room>_*` entity in Developer Tools > States for a few minutes with nothing happening:
     its last-updated time no longer moves every 20 seconds.
- **0.7.1**: warming runs end when the room reaches target instead of when the valve reports idle (D54), so
  single-valve Tado rooms learn. New constants `learn_reach_tolerance` (0.1 C) and `learn_max_run_min` (240);
  new diagnostic kind `discarded_run` with the reason. No store change. Verify on the live system:
  1. After a comfort block's warm-up, `hearth_diagnostic` shows `learned_run` once the room sensor reaches target,
     even if the valve went idle first, and `evidence_count` on `sensor.hearth_<room>_warming_rate` goes up.
  2. A room whose block ends before it gets to target (Office before it joined the boiler circuit) shows
     `discarded_run` with `block_ended` and learns nothing.
  3. A door opened after the room reached target does not discard the run.
- **0.5.0**: a frost preset no longer makes a room dormant (only VTherm central frost protection does,
  D29), so schedules with frost blocks keep running; skips never start from frost; optional workday
  sensor for bank holidays (D30). Verify on the live system that `specific_states.last_central_mode`
  reads `Frost protection` when you set central frost, and that the workday sensor's `check_date`
  service answers for future dates.
- **0.7.0**: learnt values can be exported, imported and restored per room, and a reset is reversible
  (D39-D44). New actions `hearth.export_learning` (response data, `parts`), `hearth.import_learning` (`data`,
  `parts`; validates everything before applying) and `hearth.undo_reset`; `hearth.reset_learning` now stashes what
  it clears as `last_reset` in the room store (no store migration; 0.6.x stores load unchanged); `last_reset_at` and
  `last_reset_bucket` on the warming-rate and `learning_<bucket>` sensors and `room.last_reset` in the diagnostics
  download. Also fixes the Hearth actions disappearing after a reload of the only room. Tests: 435 (was 290).
  Verify on the live system:
  1. Developer Tools > Actions, YAML mode: `hearth.export_learning` with `vtherm_entity_id` returns the room's
     blob (`format`, `version`, `parts`) and with no room returns `rooms`; the response is readable and complete
     (a full ledger is tens of kilobytes, check the UI shows all of it).
  2. `reset_learning` with `bucket: warming_rate` on a room with runs, then `undo_reset`: `sensor.hearth_<room>_warming_rate`
     `evidence_count` and constants come back, and `last_reset_at` is set while stashed and empty afterwards.
  3. Restart Home Assistant between a reset and its undo: `last_reset_at` survives and the undo still works.
  4. Reload the integration (change an option on the only room) and check the actions are still listed.
  5. Import an export into a room after deleting and re-adding it: the warning about a different VTherm appears
     only when the VTherm entity differs, and the seed hold does not restart (`seed_hold_active` stays false).
  6. A deliberately broken `data` (wrong `version`, a bad second part) is refused with a readable message and the
     room is unchanged.
  7. The `script` example in the README with `persistent_notification.create`: check how long a real export is in
     a notification before relying on it as a backup.

  Also in 0.7.0, from the first live day of the Scheduler take-over (D45-D48): `switch.hearth_paused`, changes
  made by automations no longer learnt from, and a re-enabled Scheduler source switched off again. Verify:
  8. `switch.hearth_paused` on: every `binary_sensor.hearth_<room>_dormant` reads on with reason `paused`, nothing is
     written to VTherm, the owned Scheduler schedules stay off (check the Scheduler card), and it survives a restart.
     `switch.hearth_active` off while paused still switches the schedules back on.
  9. Home Mode automation, Away branch pauses, Home branch unpauses: on unpause every room whose schedule switch is on
     switches to the preset of its current block within seconds, even after a pre-arrival warm-up set comfort, and
     `hearth_diagnostic` shows kind `resumed`.
  10. Turn a Hearth-owned Scheduler schedule on by hand: it goes off again within a second, a `hearth_diagnostic` of
      kind `scheduler_source_reenabled` appears, the room is not stood down, nothing lands in the ledger (the
      stand-down sensor's `recent_ignored_overrides` shows `scheduler_source`) and Hearth's block is back. Do it with
      the pause on and off. If you can make `switch.turn_off` fail, the repair appears two minutes later.
  11. Change a preset from the dashboard (a person) and from an automation: the first stands the room down and lands in
      the ledger; the second stands it down (`cause` `automation_preset`, `last_origin` `automation`) but adds only an
      `automation` entry to `recent_ignored_overrides`. Check that a person's edit of a preset temperature number is
      still learnt (VTherm moves its own target afterwards without the person's context, which Hearth bridges via the
      number's own state; confirm on the live system).
  12. Check the context of the Scheduler's own preset writes: the grace window assumes they carry no user.

  Observe-only rooms (D49-D53):
  13. Upgrade: the Living Room and Office have `switch.hearth_<room>_observe` off and behave exactly as before. Add a
      new test room: it starts with observe on, and a restart keeps whichever state you left it in.
  14. Observing: change the room's adaptive slope, wait for a Scheduler preset change, and confirm nothing is written to
      VTherm (compare the comfort-target sensor, which still moves, with the preset number, which does not) and no
      stand-down or ledger entry appears; `last_external_change` is still set. The Scheduler schedules stay on.
  15. Leave an observing room for a few days on its Scheduler schedules: `evidence_count` on the warming-rate sensor
      grows from the runs the Scheduler's preset changes start (the first run after each comfort block must not be
      tainted by the change that began it; if runs never count, look at the order of the preset and heating events on
      the live VTherm).
  16. A VTherm with no preset numbers: observing shows no dormancy; switching observe off shows `no_preset_entities`
      and the Repair, and the Repair clears when the numbers exist.
  17. Switch observe off with the schedule switch on: the Scheduler schedules go off, the current block is applied, and
      `next_block` shows `observe_only: false`. Switch it back on: the schedules come back on.
- **0.6.1**: `hearth.reset_learning` with `bucket: warming_rate` clears the learned warming rate (D38). Use it
  after pointing a VTherm at a different temperature sensor.
- **0.6.0**: Scheduler entities as a room's schedule source, with automatic take-over and fallback
  (D31-D37). New Tier 2 options `scheduler_entity_ids`, `warm_by_presets`, `skippable_first_morning`; four
  Repairs (`scheduler_day_conflict`, `scheduler_shared_source`, `scheduler_unsupported_action`,
  `scheduler_source_enabled`); new `next_block` attributes. Tests: 289 (was 185): 62 new pure-core and 42 new
  HA-level. Verify on the live system:
  1. Set `scheduler_entity_ids` on one room to its `switch.schedule_*` entities (schedule switch still off) and
     read `sensor.hearth_<room>_next_block`: `schedule_source` is `scheduler`, `source_blocked_reason` is empty
     and the `schedule` attribute matches the Scheduler card, with the first morning block `skippable`. Check
     the weekend schedule also appears as `non_workday`.
  2. Real Scheduler attribute shape: confirm `actions` entries read `service` / `data.preset_mode` for these
     entities (the derivation also accepts `service_data`), and that a slot with a single-time `timeslots`
     string (no stop) behaves.
  3. Turn the room's schedule switch on: both source switches go off within a second and no Repair appears
     after two minutes. Turn it off: both come back on. Do the same with `switch.hearth_active`.
  4. Edit a slot in the Scheduler card while Hearth owns the room: the schedule updates without a reload and
     the sources stay off.
  5. A source that also targets a non-Hearth climate entity raises `scheduler_shared_source` and is left on.
  6. Restart Home Assistant with the schedule switch on: the sources are off again after startup, and a
     restart with Scheduler not yet loaded keeps using the last derived schedule.
  7. Removing the Hearth room (or unloading the integration) turns the source schedules back on.
  8. hassfest and HACS validation accept `scheduler` (a custom integration, not core) in `after_dependencies`.

## Known gaps and follow-ups

- The options flow does not edit the schedule (by design in v1); `hearth.set_schedule` and YAML are the
  editors. A schedule editor UI would be a later addition.
- `switch.hearth_active` belongs to the first-loaded config entry (D3).
- Repairs are raised only for the Scheduler source (0.6.0). Everything else is `hearth_diagnostic` events
  and log lines; a repair for the missing preset numbers would be a natural addition.
- Local development: HA 2026.9 needs Python 3.14.2. The core tests run under any Python 3.11+. A
  Python 3.13 venv with `pytest-homeassistant-custom-component==0.13.316` (HA 2026.2) was used for local
  runs during the build and behaved identically except for the lingering-timer check, which is stricter in
  2026.9 and is now handled.
