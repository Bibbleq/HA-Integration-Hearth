# Hearth

[![CI](https://github.com/Bibbleq/HA-Integration-Hearth/actions/workflows/ci.yml/badge.svg)](https://github.com/Bibbleq/HA-Integration-Hearth/actions/workflows/ci.yml)
[![hacs](https://img.shields.io/badge/HACS-custom-orange.svg)](https://hacs.xyz)

Adaptive comfort and forecast-aware heating control for
[Versatile Thermostat](https://github.com/jmcollin78/versatile_thermostat) (VTherm),
as a Home Assistant custom integration.

Hearth sits *outside* VTherm and drives it through public entities only:
it writes the per-preset temperature numbers (`number.<vtherm>_preset_comfort_temp`
and friends) and switches presets with `climate.set_preset_mode`. It never writes a
raw setpoint. Disable Hearth and VTherm carries on with the last preset temperatures.

Four mechanisms, delivered as four phases, each behind its own switch:

| Phase | Mechanism | Default |
|---|---|---|
| 1 | **Adaptive comfort offset**: the comfort target drifts with a running mean of outdoor temperature (EN 16798 / ASHRAE 55 acclimatisation) | on |
| 2 | **Forecast skip** and **setback depth**: skip the morning heat when the day will do it for free; shallower overnight setback before very cold mornings | off |
| 3 | **Warm-by schedule** with **learned preheat**: blocks say when a room should *be* warm; Hearth learns how fast each room warms and starts early enough | off |
| 4 | **Override learning**: manual changes are recorded in a correction ledger (record-only; application is a separate flag, off) | off |

> **Status: early.** All four mechanisms are implemented and covered by tests against a simulated
> VTherm, but Hearth has not yet completed a heating season on a live system. Start with phase 1 on one
> room and keep an eye on it. Hearth is an independent project, not affiliated with Versatile Thermostat.

See [DECISIONS.md](DECISIONS.md) for what was chosen where the design left options open, and
[BUILD-REPORT.md](BUILD-REPORT.md) for what is done, untested and worth checking on a live system.

## Install

**HACS (custom repository)**

1. HACS > Integrations > three dots > *Custom repositories*
2. Add `https://github.com/Bibbleq/HA-Integration-Hearth`, category *Integration*
3. Install *Hearth*, restart Home Assistant

**Manual**: copy `custom_components/hearth` into your `config/custom_components/` and restart.

Then *Settings > Devices & services > Add integration > Hearth*. One Hearth room per VTherm.

### VTherm prerequisites

- VTherm 8.0 or newer (built and verified against 10.4).
- The VTherm must expose its own preset temperature numbers. In the VTherm's config, **untick
  "use central configuration" for preset temperatures**; otherwise `number.<vtherm>_preset_comfort_temp`
  does not exist and Hearth reports the room as dormant (`no_preset_entities`).
- Presence, window, safety and overpowering stay VTherm's job. Hearth is dormant while any of them is active.
- The TRV side must be a plain actuator with no early-start or smart schedule of its own (on a Tado estate: flat
  temperatures in the Tado app, Early Start off). Hearth's preheat must be the only warm-up brain.

## Configuration tiers

**Tier 1, runtime entities** (per room unless noted): change them from the UI at any time.

| Entity | Default | Purpose |
|---|---|---|
| `switch.hearth_active` | on | Global kill switch (one for the whole integration). Off hands every room back: Scheduler schedules Hearth switched off are switched back on |
| `switch.hearth_paused` | off | Global pause (one for the whole integration). On makes every room dormant (`paused`) but keeps ownership: Scheduler schedules Hearth switched off **stay off**. Off puts each room straight back on its current schedule block. See [Working with occupancy / home-away automations](#working-with-occupancy--home-away-automations) |
| `switch.hearth_<room>_adaptive_comfort` | on | Phase 1 |
| `switch.hearth_<room>_forecast_skip` | off | Phase 2 |
| `switch.hearth_<room>_setback_depth` | off | Phase 2 |
| `switch.hearth_<room>_schedule` | off | Phase 3: Hearth owns preset switching for this room |
| `switch.hearth_<room>_preheat` | off | Phase 3: warm-by blocks start early (needs schedule on) |
| `switch.hearth_<room>_override_learning` | off | Phase 4: record overrides in the ledger |
| `switch.hearth_<room>_apply_learned_corrections` | off | Phase 4: apply bounded corrections from the ledger |
| `number.hearth_<room>_adaptive_slope` | 0.15 | C of comfort per C of running mean above/below 10 C |
| `number.hearth_<room>_skip_threshold` | 18 C | Forecast high at or above which the morning heat is skipped |
| `number.hearth_<room>_max_offset` | 1.5 C | Cap on the total adaptive offset |
| `number.hearth_<room>_min_indoor_floor` | 16 C | Below this, no skip starts and any active skip aborts |
| `number.hearth_<room>_cold_morning_threshold` | 2 C | Coldest 06:00-09:00 forecast hour below which the overnight setback is shallower |

**Tier 2, config entry options** (*Configure* on the integration): target VTherm, room name, weather
entity, outdoor sensor override, workday sensor, baseline comfort temperature, comfort band min/max (the hard clamps),
affected presets, baseline boost temperature, solar-gain flag, skip decision / skip end / bedtime decision
times. The schedule (phase 3) is also stored here, edited via the `hearth.set_schedule` service, or taken from
HA Scheduler entities instead (`scheduler_entity_ids`, `warm_by_presets`, `skippable_first_morning`; see
[Using the Scheduler card as the schedule source](#using-the-scheduler-card-as-the-schedule-source)).

**Tier 3, constants**: everything else lives in one `DEFAULTS` dict in `const.py`. Any key can be
overridden from `configuration.yaml`:

```yaml
hearth:
  advanced:
    alpha: 0.8                 # running-mean weight
    t_ref: 10.0                # C, running mean at which no offset applies
    standdown_timeout_min: 180
    max_forecast_age_hours: 3
    safety_factor: 1.15
    max_preheat_min: 120
    learn_from_automations: false   # true: learn from automation-made changes too
    source_reenable_grace_s: 15     # a preset change this soon after a Scheduler source comes on is the Scheduler's
```

Unknown keys are ignored with a warning. Nothing in Tier 3 is required for sane behaviour.

## Entities

Each room is a device named *Hearth \<Room\>*. Diagnostics (read-only):

| Entity | Meaning |
|---|---|
| `sensor.hearth_<room>_running_mean_outdoor` | T_rm in C, with today's partial mean, sample count and recent daily means |
| `sensor.hearth_<room>_comfort_target` | Computed comfort target (pre-quantise) with the applied offset, quantised value and last write |
| `sensor.hearth_<room>_skip_status` | idle / preview / active / aborted, with reason, threshold and forecast basis |
| `sensor.hearth_<room>_skip_preview` | Today until the decision time, then tomorrow: likely / unlikely / unknown, with the forecast basis |
| `sensor.hearth_<room>_setback_status` | idle / active, with the coldest-morning forecast, eco base and target, restore time |
| `sensor.hearth_<room>_warming_rate` | C/h at the current outdoor temperature, with model constants and run count |
| `sensor.hearth_<room>_next_block` | Phase 3: "comfort by 06:30, preheat est. 05:52". Attributes include `schedule_source` (`scheduler` / `explicit` / `none`), `source_entities`, `source_blocked_reason` and `derived_at` |
| `sensor.hearth_<room>_stand_down` | none / until (local timestamp), with cause and the last external change |
| `sensor.hearth_<room>_learning_<bucket>` | Phase 4: decayed mean bias in C per bucket (`preheat_shortfall`, `slope_error`, `skip_failure`, `baseline_error`) with evidence count and the correction that would apply |
| `binary_sensor.hearth_<room>_dormant` | On when Hearth is leaving the room alone, with reason (away / central_frost / window / safety / overpowering / off / unavailable / disabled / paused / no_preset_entities) |

Hearth also fires `hearth_diagnostic` events (seeding, outdoor sensor loss, refused writes, learning imports and
undos) and provides a config-entry diagnostics download. The warming-rate sensor and the four `learning_<bucket>`
sensors carry `last_reset_at` and `last_reset_bucket` while a reset can still be undone.

## Services

| Service | Fields | What it does |
|---|---|---|
| `hearth.override` | room, `duration` | Stand Hearth down deliberately ("heat normally for 3 h"). Ends an active skip and restores the preset. Works whether or not override learning is on |
| `hearth.recompute` | room (optional), `force` | Force an adaptive recompute and write |
| `hearth.refresh_forecast` | room (optional) | Fetch the forecast now |
| `hearth.set_schedule` | room, `schedule` | Replace a room's warm-by schedule (phase 3). Refused for a room that takes its schedule from Scheduler entities |
| `hearth.reset_learning` | room, `bucket` (optional) | Clear the ledger, or one bucket (phase 4). `bucket: warming_rate` instead clears the learned warming rate and any run in progress, back to the 1.0 C/h default; do this after changing the VTherm's temperature sensor. An empty bucket never touches the warming rate. What is cleared is kept so `hearth.undo_reset` can bring it back |
| `hearth.export_learning` | room (optional), `parts` (optional) | Return a room's learnt values as response data: `warming_rate`, `ledger`, `running_mean`. Name one room for its data directly, none for every room under `rooms`. Writes nothing to disk |
| `hearth.import_learning` | room, `data`, `parts` (optional) | Replace a room's learnt values with data from `export_learning`. Everything is checked before anything is applied |
| `hearth.undo_reset` | room | Put back what the last `reset_learning` cleared in that room |

`room` is either `entry_id` (config entry selector) or `vtherm_entity_id` (the climate entity the room drives).

## Backing up and moving learning

What Hearth learns lives in one small file per room (`.storage/hearth.room.climate_living_room` for `climate.living_room`): the **warming rate**
(how fast the room warms, from clean heating runs), the **override ledger** (your manual corrections) and the
**running mean** of outdoor temperature. A normal Home Assistant backup already includes it, but restoring a
backup is all or nothing: it rolls back everything else in Home Assistant too. Hearth adds a way to take the
learnt values out, put them back, and undo a reset, per room.

- **Diagnostics download** (the room's device page, *Download diagnostics*) contains the learnt values for
  reading: `room.mechanisms.warming`, `room.ledger` and `room.running_mean`, plus `room.last_reset` while a reset
  can be undone. It is a snapshot to look at or keep, not something to feed back in as it stands, though its
  three parts have the same shape as the export (see the last example below).
- **`hearth.export_learning`** returns the values as action response data. Nothing is written to disk. With a
  room named (`entry_id` or `vtherm_entity_id`) the response is that room's data:

  ```json
  {"format": "hearth.learning", "version": 1, "exported_at": "2026-10-22T07:30:00+00:00", "hearth_version": "0.7.0",
   "room": "Living Room", "vtherm": "climate.living_room",
   "parts": {"warming_rate": {...}, "ledger": {...}, "running_mean": {...}}}
  ```

  With no room named you get every room: `{"rooms": {"climate.living_room": {...}, "climate.bedroom": {...}}}`.
  `parts` (a list of `warming_rate`, `ledger`, `running_mean`) limits what is exported; the default is all
  three. A part with nothing learnt yet is left out.
- **`hearth.import_learning`** takes a room and `data` (the object above), and optionally `parts` (default: every
  part the data holds). The whole object is checked first (format, version, every selected part) and nothing is
  applied unless all of it is good. Each imported part then *replaces* the room's current one. Importing a
  warming rate also drops a heating run in progress. An imported running mean keeps its own seeding time, so it
  does not start another 48 h hold. Data exported from a different VTherm is allowed (that is how you restore a
  rebuilt room) but logs a warning. The room is saved at once and recomputed.
- **`hearth.reset_learning`** now keeps what it is about to clear, and **`hearth.undo_reset`** puts it back.
  Only the latest reset is kept, and it is gone once undone. Undoing a single-bucket reset restores just that
  bucket; anything learnt since the reset is kept alongside it. A reset that clears nothing leaves the previous
  undo in place.

Try it in *Developer Tools > Actions* (YAML mode shows the response):

```yaml
action: hearth.export_learning
data:
  vtherm_entity_id: climate.living_room
```

To restore, paste the response's object under `data:`; `parts` is optional:

```yaml
action: hearth.import_learning
data:
  vtherm_entity_id: climate.living_room
  parts: [warming_rate]
  data:
    format: hearth.learning
    version: 1
    parts:
      warming_rate:
        a: 2.1
        b: 0.05
        runs: [[10.0, 1.6], [14.0, 1.4], [18.0, 1.2]]
        max_runs: 30
```

The export is far too large for an `input_text` helper (255 characters), so do not try to keep it there. In
a script, take it with `response_variable` and hand it to whatever should keep it:

```yaml
script:
  hearth_backup_living_room:
    sequence:
      - action: hearth.export_learning
        data:
          vtherm_entity_id: climate.living_room
        response_variable: learning
      - action: persistent_notification.create
        data:
          title: Hearth learning, Living Room
          message: "{{ learning | to_json }}"
```

A `notify` action, or any integration that writes a file, can take `{{ learning | to_json }}` in the same way
if you want the backup somewhere that survives a restart. To use the diagnostics download instead, wrap its three
parts: `data: {format: hearth.learning, version: 1, parts: {warming_rate: <room.mechanisms.warming>,
ledger: <room.ledger>, running_mean: <room.running_mean>}}`.

## Working with occupancy / home-away automations

Hearth predicts: it moves comfort and presets from the weather, the schedule and what it has learnt. Whether
anyone is home is something you know, so it is better left to your own automations, which should decide
*whether Hearth acts*, not fight it. The pattern:

- **Away / Holiday branch:** `switch.turn_on` on `switch.hearth_paused`, then do what the away mode needs (a
  temperature cap, a deep setback, the eco preset). Hearth is dormant while paused: it writes nothing, learns
  nothing, ignores the changes you make, and keeps its Scheduler schedules switched off.
- **Home / Guest branch:** `switch.turn_off` on `switch.hearth_paused`. Each room goes back to its current schedule
  block at once (even if a pre-arrival warm-up had set comfort), and the adaptive comfort recompute runs. Do not
  turn on the Scheduler schedules of rooms Hearth owns; if an automation does it anyway Hearth switches them off
  again and puts its own block back.

```yaml
- choose:
    - conditions: "{{ is_state('input_select.home_mode', ['Away', 'Holiday']) }}"
      sequence:
        - action: switch.turn_on
          target: {entity_id: switch.hearth_paused}
        # ...away temperature cap, deep setback...
  default:
    - action: switch.turn_off
      target: {entity_id: switch.hearth_paused}
```

Pausing is not the kill switch. `switch.hearth_active` off hands every room back (Scheduler schedules come on), so
use it when you want Hearth out of the picture; `switch.hearth_paused` on keeps ownership and leaves them off. With
both set, `switch.hearth_active` off wins. The pause is remembered across restarts.

Unpausing ends any stand-down (it is a fresh start) and applies the current schedule block of every room whose
schedule switch is on; a skip, setback or preheat starts only by its usual rules. Leaving other kinds of dormancy
(a window closing, a presence change) is unchanged: whatever was set meanwhile stands until the next block.

## Enabling each phase

Phases are meant to soak for a few weeks each. Everything is per room.

1. **Adaptive comfort** is on from install. For the first 48 h after seeding the offset is held at zero.
   Watch `sensor.hearth_<room>_comfort_target` against the VTherm's comfort number.
2. **Forecast skip / setback**: configure a weather entity (options), then turn on
   `switch.hearth_<room>_forecast_skip` and/or `switch.hearth_<room>_setback_depth`. Your existing
   HA Scheduler entries stay in charge of presets; a Scheduler slot firing mid-skip ends the skip.
   Check `sensor.hearth_<room>_skip_preview` in the evening and `skip_status` in the morning.
3. **Schedule and preheat**: give the room a schedule, then turn on `switch.hearth_<room>_schedule`. Either set an
   explicit schedule with `hearth.set_schedule` and disable that room's HA Scheduler entries yourself, or point the
   room at its Scheduler schedules (`scheduler_entity_ids` in the room options) and let Hearth do the hand-over: turn
   the schedule switch on and Hearth switches the source schedules off itself; turn it off and it switches them back
   on. Once the preset switching looks right, turn on `switch.hearth_<room>_preheat`. The warming-rate model uses a
   conservative 1.0 C/h until it has five clean heating runs.
4. **Override learning**: turn on `switch.hearth_<room>_override_learning`. From then on a manual preset
   or temperature change stands Hearth down for that room (until the next schedule block or 180 min) and is
   recorded in the ledger. The `learning_*` sensors show the biases and the correction each bucket would
   apply. `apply_learned_corrections` is a second-winter flag: bounded corrections (+/- 1 C on targets,
   +/- 25 % on the preheat safety factor) after three consistent events, always inside the band clamps.
   **Only a person's changes are learnt from.** A change counts as a person's when the state change carries a
   user (`context.user_id`: the dashboard, the app, voice). A change with no user (an automation, a script,
   Scheduler, another integration) still stands Hearth down for the room, so Hearth does not fight it, but it
   is not recorded in the ledger; it appears in the stand-down sensor's `recent_ignored_overrides` with reason
   `automation`, and `last_origin` on that sensor says `human` or `automation`. Set `learn_from_automations: true`
   under `hearth: advanced:` to learn from both, as before 0.7.0. Hearth's own writes are never either.

### Schedule format

```yaml
service: hearth.set_schedule
data:
  vtherm_entity_id: climate.living_room
  schedule:
    mon: &weekday
      - { warm_by: "06:30", preset: comfort, skippable: true }
      - { at: "09:00", preset: eco }
      - { warm_by: "17:00", preset: comfort }
      - { at: "22:30", preset: eco }
    tue: *weekday
    wed: *weekday
    thu: *weekday
    fri: *weekday
    sat: &weekend
      - { warm_by: "08:30", preset: comfort, skippable: true }
      - { at: "22:00", preset: eco }
    sun: *weekend
    # non_workday: optional, see below
```

`warm_by` blocks get preheat; `at` blocks fire at the stated time. `skippable` marks blocks the forecast
skip may suppress. Presets: `frost`, `eco`, `comfort`, `boost`. One year-round schedule per room; the
adaptive offset, skip and preheat handle the seasons.

**Bank holidays.** Set a Workday integration sensor in the room options. A Monday to Friday that the
sensor says is not a workday uses the schedule's `non_workday` blocks if it has them, otherwise
Saturday's. Without a workday sensor every date follows its weekday.

**Frost in a schedule is normal.** Hearth stays active through frost blocks it applied, and a frost
preset set by hand stands until the next block like any other manual change. Only VTherm's central
"Frost protection" mode (holiday) makes Hearth dormant. Eco is the better resting state between
comfort blocks: recovering from frost can take longer than the 120 minute preheat cap allows.

## Using the Scheduler card as the schedule source

If you already manage heating schedules in the HACS [Scheduler](https://github.com/nielsfaber/scheduler-component)
component and its card, Hearth can read them instead of keeping a second copy. In the room's options, set
**Scheduler schedules** (`scheduler_entity_ids`) to the `switch.schedule_*` entities that drive that room.

**What Hearth reads.** Each Scheduler entity carries its whole week in its attributes whether it is on or off:
`weekdays`, `timeslots` and `actions`. Hearth turns every timeslot start into one block at that time, with the
preset from the matching action. The slot's stop time is ignored; the next block ends the previous one.

- Every action must be **Set preset** (`climate.set_preset_mode`) with `frost`, `eco`, `comfort` or `boost`.
  Anything else, and sunrise/sunset slots, make that schedule unusable (see Repairs below).
- Weekdays can be explicit (`mon`..`sun`) or the Scheduler shorthands `daily`, `workday` and `weekend`.
- Slots whose preset is in **Presets that count as warm-by** (`warm_by_presets`, default comfort and boost) become
  `warm_by` blocks, so they get preheat. All other slots are plain `at` blocks.
- With **First morning block is skippable** (`skippable_first_morning`, default on) the first warm-by block of each
  day that falls before 12:00 (`morning_cutoff`) is marked `skippable` for the forecast skip.
- A schedule that covers exactly Saturday and Sunday (or `weekend`) also supplies the `non_workday` blocks used on
  bank holidays (see Bank holidays under Schedule format).
- Each day must belong to exactly one schedule. A day no schedule covers has no blocks, so the previous day's last
  preset carries over.

The Living Room example: `switch.schedule_living_room_weekday` (Mon to Fri: eco, comfort from 06:30, eco from 09:00,
comfort from 16:00, eco from 22:00) plus `switch.schedule_living_room_weekend` (Sat and Sun) gives a full week, and
the weekend schedule doubles as the bank-holiday schedule.

**Live updates.** Hearth re-reads the entities on every state or attribute change, so editing a schedule in the
Scheduler card takes effect immediately, even while Hearth owns the room. If a source goes unavailable or is
missing, Hearth keeps the last good schedule (it is remembered across restarts) and fires a `hearth_diagnostic`
event of kind `scheduler_source_missing`. With sources configured, the derived schedule always replaces any stored
blob, and `hearth.set_schedule` is refused for that room. Clear the option to use an explicit schedule again.

**Hand-over and fallback.** While the room's schedule switch is off, nothing changes: Scheduler keeps running the
heating and you can read the derived schedule in `sensor.hearth_<room>_next_block` to check it.

- Turning `switch.hearth_<room>_schedule` on (with `switch.hearth_active` on) makes Hearth call `switch.turn_off` on
  every source schedule. It does the same at startup if the switch was already on.
- Turning the schedule switch off, turning `switch.hearth_active` off, reloading or unloading the integration, or
  deleting the room makes Hearth call `switch.turn_on` on every source schedule, so Scheduler is the fallback.
  Dormancy (window, away, frost, safety), the global pause (`switch.hearth_paused`) and a stand-down are temporary and do
  not hand back.
- If a source is switched back on while Hearth owns the room (an automation that turns all schedules on, a tap in
  the Scheduler card, a Scheduler reload), Hearth calls `switch.turn_off` on it again straight away, whether or not
  it is paused. The Scheduler fires its current slot the moment it is enabled; a preset change within
  `source_reenable_grace_s` (15 s) of a source coming on is put down to the Scheduler: no stand-down, no ledger
  entry (ignored with reason `scheduler_source`), and Hearth puts its own current block back (unless paused or
  dormant). This holds whichever arrives first, the switch event or the preset write.
- Hearth will not take over a schedule that also drives other climate entities unless each of them is a Hearth
  room with its schedule switch on, because switching it off would stop those rooms. It also will not take over when
  it has no usable schedule yet. Hearth cannot hand back if the integration files are deleted while it owns a room;
  turn the schedules back on in the Scheduler card in that case.

**Repairs.** Hearth raises these in *Settings > Repairs* and clears them when the cause goes away. A missing or
unavailable source is only a diagnostic event and a log line.

| Repair | Meaning |
|---|---|
| `scheduler_day_conflict` | Two source schedules cover the same day. Hearth does not apply the change and keeps the last good schedule |
| `scheduler_shared_source` | A source also drives climate entities that are not Hearth rooms with the schedule switch on. Hearth does not take it over or apply it |
| `scheduler_unsupported_action` | A slot is not a preset change (or has a sunrise/sunset time). That schedule is unusable and the change is not applied |
| `scheduler_source_enabled` | A source schedule is still on two minutes after Hearth took the room over or switched it off again, so two systems may be changing presets (Hearth's own `switch.turn_off` is not working). Turn it off, or hand the room back |

## Safety behaviour

- Every preset temperature write goes through one clamped writer. Comfort and boost never leave
  `[band_min, band_max]`; eco is never lowered by Hearth and never raised above `band_max` or the comfort target.
- A stale forecast (older than 3 h) disables skip and setback decisions; heating proceeds normally.
- Outdoor sensor loss freezes the running mean and holds the offset.
- VTherm presence away, central frost protection, window, safety or overpowering makes Hearth dormant for that room.
- `min_indoor_floor` breach cancels any Hearth-originated eco state immediately.
- Restart is recompute-from-state: Hearth never re-fires actions blindly.

## Development

Pure maths lives in `custom_components/hearth/core/` with no Home Assistant imports and runs under plain
`pytest`. The HA glue tests need `pytest-homeassistant-custom-component` (see `requirements_test.txt`).
CI runs pytest, hassfest and HACS validation on every push.
