# Hearth

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
- Before enabling phase 3, disable Tado Early Start on the zone (two preheat brains must not coexist).

## Configuration tiers

**Tier 1, runtime entities** (per room unless noted): change them from the UI at any time.

| Entity | Default | Purpose |
|---|---|---|
| `switch.hearth_active` | on | Global kill switch (one for the whole integration) |
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
entity, outdoor sensor override, baseline comfort temperature, comfort band min/max (the hard clamps),
affected presets, baseline boost temperature, solar-gain flag, skip decision / skip end / bedtime decision
times. The schedule (phase 3) is also stored here, edited via the `hearth.set_schedule` service.

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
| `sensor.hearth_<room>_next_block` | Phase 3: "comfort by 06:30, preheat est. 05:52" |
| `sensor.hearth_<room>_stand_down` | none / until (local timestamp), with cause and the last external change |
| `sensor.hearth_<room>_learning_<bucket>` | Phase 4: decayed mean bias in C per bucket (`preheat_shortfall`, `slope_error`, `skip_failure`, `baseline_error`) with evidence count and the correction that would apply |
| `binary_sensor.hearth_<room>_dormant` | On when Hearth is leaving the room alone, with reason (away / frost / window / safety / overpowering / off / disabled / no_preset_entities) |

Hearth also fires `hearth_diagnostic` events (seeding, outdoor sensor loss, refused writes) and provides a
config-entry diagnostics download.

## Services

| Service | Fields | What it does |
|---|---|---|
| `hearth.override` | room, `duration` | Stand Hearth down deliberately ("heat normally for 3 h"). Ends an active skip and restores the preset. Works whether or not override learning is on |
| `hearth.recompute` | room (optional), `force` | Force an adaptive recompute and write |
| `hearth.refresh_forecast` | room (optional) | Fetch the forecast now |
| `hearth.set_schedule` | room, `schedule` | Replace a room's warm-by schedule (phase 3) |
| `hearth.reset_learning` | room, `bucket` (optional) | Clear the ledger, or one bucket (phase 4) |

`room` is either `entry_id` (config entry selector) or `vtherm_entity_id` (the climate entity the room drives).

## Enabling each phase

Phases are meant to soak for a few weeks each. Everything is per room.

1. **Adaptive comfort** is on from install. For the first 48 h after seeding the offset is held at zero.
   Watch `sensor.hearth_<room>_comfort_target` against the VTherm's comfort number.
2. **Forecast skip / setback**: configure a weather entity (options), then turn on
   `switch.hearth_<room>_forecast_skip` and/or `switch.hearth_<room>_setback_depth`. Your existing
   HA Scheduler entries stay in charge of presets; a Scheduler slot firing mid-skip ends the skip.
   Check `sensor.hearth_<room>_skip_preview` in the evening and `skip_status` in the morning.
3. **Schedule and preheat**: set the room's schedule with `hearth.set_schedule`, turn on
   `switch.hearth_<room>_schedule`, then disable that room's HA Scheduler entries. Once the preset
   switching looks right, turn on `switch.hearth_<room>_preheat`. The warming-rate model uses a
   conservative 1.0 C/h until it has five clean heating runs.
4. **Override learning**: turn on `switch.hearth_<room>_override_learning`. From then on a manual preset
   or temperature change stands Hearth down for that room (until the next schedule block or 180 min) and is
   recorded in the ledger. The `learning_*` sensors show the biases and the correction each bucket would
   apply. `apply_learned_corrections` is a second-winter flag: bounded corrections (+/- 1 C on targets,
   +/- 25 % on the preheat safety factor) after three consistent events, always inside the band clamps.

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
```

`warm_by` blocks get preheat; `at` blocks fire at the stated time. `skippable` marks blocks the forecast
skip may suppress. Presets: `frost`, `eco`, `comfort`, `boost`. One year-round schedule per room; the
adaptive offset, skip and preheat handle the seasons.

## Safety behaviour

- Every preset temperature write goes through one clamped writer. Comfort and boost never leave
  `[band_min, band_max]`; eco is never lowered by Hearth and never raised above `band_max` or the comfort target.
- A stale forecast (older than 3 h) disables skip and setback decisions; heating proceeds normally.
- Outdoor sensor loss freezes the running mean and holds the offset.
- Any VTherm away / frost / window / safety / overpowering state makes Hearth dormant for that room.
- `min_indoor_floor` breach cancels any Hearth-originated eco state immediately.
- Restart is recompute-from-state: Hearth never re-fires actions blindly.

## Development

Pure maths lives in `custom_components/hearth/core/` with no Home Assistant imports and runs under plain
`pytest`. The HA glue tests need `pytest-homeassistant-custom-component` (see `requirements_test.txt`).
CI runs pytest, hassfest and HACS validation on every push.
