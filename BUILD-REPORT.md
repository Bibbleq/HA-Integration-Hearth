# Hearth - build report

Built 2026-09-19 from the design spec (draft 1.0, August 2026). All four phases are implemented and
pushed to `main`, each as its own commit. CI (GitHub Actions) runs pytest on Home Assistant 2026.9.3 /
Python 3.14, hassfest, and HACS validation.

| Check | State |
|---|---|
| pytest (176 tests: 121 pure-core, 55 HA-level) | green on CI |
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
5. **Dormant reasons.** Open a window, set presence away, switch to frost: the dormant sensor should follow
   with the right reason each time. If one does not, dump the VTherm's attributes and compare with D1.
6. **Forecast cache.** Set a weather entity, call `hearth.refresh_forecast`, and check
   `sensor.hearth_<room>_skip_status` attributes `forecast_fresh` and `forecast_fetched_at`, and the
   diagnostics download for the normalised cache (hourly count, daily highs, conditions).
7. **Chattiness.** The room store is written at most every 30 s while VTherm attributes change. Watch
   `.storage/hearth.room.*` size and consider recorder excludes for the Hearth diagnostic sensors if the
   attributes are noisy in history.
8. **Before phase 3 on a room**: disable that room's HA Scheduler entries (Tado is already a flat-temperature
   actuator with no early start, so nothing to change there), set the schedule with `hearth.set_schedule`,
   then turn on the schedule switch and check `sensor.hearth_<room>_next_block`.
9. **HACS install path**: add the repo in HACS as a custom repository (category Integration), or copy
   `custom_components/hearth` manually.

## Known gaps and follow-ups

- The options flow does not edit the schedule (by design in v1); `hearth.set_schedule` and YAML are the
  editors. A schedule editor UI would be a later addition.
- `switch.hearth_active` belongs to the first-loaded config entry (D3).
- No repair issues are raised (only `hearth_diagnostic` events and log lines). A repair for the missing
  preset numbers would be a natural addition.
- Local development: HA 2026.9 needs Python 3.14.2. The core tests run under any Python 3.11+. A
  Python 3.13 venv with `pytest-homeassistant-custom-component==0.13.316` (HA 2026.2) was used for local
  runs during the build and behaved identically except for the lingering-timer check, which is stricter in
  2026.9 and is now handled.
