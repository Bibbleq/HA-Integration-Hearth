# Hearth - build decisions

Decisions taken during the build where the design spec left a choice open,
needed an assumption, or where the implementation deviates from the spec.
Numbered in the order they were taken. Spec section references in brackets.

## D1. VTherm attribute names (spec 12.1)

Verified against Versatile Thermostat 10.4.0 (`custom_components/versatile_thermostat/base_thermostat.py`
and the feature managers). Hearth reads the climate entity's state attributes:

| Need | Attribute path | Values |
|---|---|---|
| Preset | `preset_mode` | `none`, `frost`, `eco`, `comfort`, `boost`, `safety`, `power`, `activity` |
| Indoor temp | `current_temperature` | float |
| Outdoor temp | `specific_states.ext_current_temperature` | float or None |
| Temperature slope | `specific_states.temperature_slope` | C/h, **only populated when VTherm's automatic window detection is configured** (it comes from the window auto-detect algorithm) |
| Window | `window_manager.window_state` | `on` / `off` (block only present when window detection is configured) |
| Safety | `safety_manager.safety_state` | `on` / `off` |
| Overpowering | `power_manager.overpowering_state` | `on` / `off` / `unknown` |
| Presence | `presence_manager.presence_state` | `on` / `off` / `home` / `not_home` |

The adapter (`vtherm.py`) reads each nested block and falls back to a root-level key of the same name, so
older VTherm layouts still work. A missing block reads as "not active".

Because `temperature_slope` is only available with window auto-detection on, **Hearth computes its own warming
rate from `current_temperature` samples during heating runs** (D12) rather than depending on that attribute. It
is still exposed in diagnostics when present.

VTherm events (`versatile_thermostat_preset_event` etc.) are not used; Hearth subscribes to state changes of
the climate entity, which carries everything needed and is version-stable.

## D2. Preset temperature entities

VTherm creates `number.<vtherm_slug>_preset_<preset>_temp` (unique_id `<device>_preset_<preset>_temp`) **only
when the VTherm does not inherit preset temperatures from the central configuration**. Hearth resolves them
through the entity registry (same config entry as the climate entity, unique_id suffix match) and falls back
to the naming convention. If the comfort number does not exist the room is dormant with reason
`no_preset_entities` and a diagnostic event fires. On the live estate this means unticking "use central
configuration" for preset temperatures on each VTherm Hearth should drive.

## D3. Global switch ownership

`switch.hearth_active` has to belong to some config entry. It is created by the first Hearth entry that loads
and its state lives in a separate store (`hearth.global`) so it survives that entry being removed. If the
owning entry is removed, the switch reappears under whichever entry loads first next time; the stored state
is unaffected.

## D4. Entity naming

Each room is an HA device named `Hearth <Room>`, entities use `has_entity_name`, so ids come out as
`sensor.hearth_living_room_running_mean_outdoor`, `switch.hearth_living_room_adaptive_comfort` and so on.
The spec's `sensor.<room>_...` shape would collide with VTherm's own per-room entities; the `hearth_`
namespace keeps them apart and groups them in the UI.

## D5. Tier 1 values persist in the room store, not RestoreEntity

Switches and numbers write their values into the room's `Store` and the controller reads them from there.
That keeps the controller usable before the entity platforms have loaded and makes the values part of the
diagnostics dump. Defaults: adaptive on, every other mechanism off, apply-learning off.

## D6. "Write only when the quantised value changes" means since Hearth's last write

The controller compares the desired quantised value against `last_written[preset]` (persisted). It writes when
that differs, or when there is no record yet and the entity does not already show the value. It does **not**
re-assert a value the user changed by hand: a manual edit of the comfort number stands until the computed
target moves. `hearth.recompute` with `force: true` re-asserts. This is what makes restart idempotent (spec 10)
and stops Hearth fighting manual changes before phase 4's stand-down exists.

## D7. Daily mean accumulator

Outdoor samples are taken every `outdoor_sample_interval_min` (15) from the controller tick. The tracker keeps
the current day's accumulator and one completed day; the roll at `daily_recompute_time` (00:10 local) folds the
completed day into T_rm exactly once. If HA was down across midnight the first tick after 00:10 rolls the
partial day that was captured. Days with no samples at all are not folded (T_rm holds).

## D8. Seeding

Seed from recorder long-term statistics (daily means) of the configured outdoor sensor, else raw history,
else the current reading. When the outdoor source is the VTherm attribute there is no entity to query, so it
seeds from the current reading. The 48 h hold (`seed_hold_hours`) applies in every case.

## D9. Boost as an affected preset

When boost is in the affected presets, its target is `base_boost_temp` (Tier 2, default 22.0) plus the same
adaptive offset, under the same clamps. Comfort-only remains the default.

## D10. Clamp rules for eco writes (setback depth)

`band_min`/`band_max` are the comfort clamps. Eco sits below `band_min` by design, so eco writes use the rule:
never below the eco value Hearth read before touching it, never above `band_max`, and never above the current
comfort target. The writer refuses anything else. No path writes outside `[band_min, band_max]` for comfort or
boost.

## D11. Weather entity default (spec 12.3)

Per room, optional. When empty, Hearth uses any other room's configured weather entity, else the first
`weather.*` entity in HA. So one configured room effectively sets the shared default.

## D12. Warming-rate learner input

Own computation from `current_temperature` sampled at each tick while `hvac_action == heating`, in an affected
preset, with the room in scope and no override. Runs shorter than 20 min or with a rise under 0.3 C are
discarded, as is any run touched by a window/safety/overpowering/away transition or a manual change.
Rate = rise / hours; delta = target - mean outdoor during the run. Fit is ordinary least squares with `b`
forced non-negative. Recorded in `WarmingModel`, capped at 30 runs.

## D13. Skip mechanism and HA Scheduler (spec 12.2)

Preset switch to eco, as preferred. HA Scheduler only fires at slot boundaries, so it does not re-assert
comfort mid-skip; when it does fire (e.g. a 16:00 comfort slot before a 16:30 skip end) Hearth sees an
external preset change, treats it as manual intervention and ends the skip without restoring. That is the
spec's "override logic takes over" behaviour and needs no temp-write fallback.

## D14. Decisions use catch-up windows

Decision times (skip 06:45, bedtime 21:30, preview 21:00, setback restore 09:00) are evaluated from state on
every tick with a "decided for date" marker, so a restart shortly after a decision time still makes the
decision. A missed skip or setback decision is made late only within `*_late_decision_window_min` (120 min);
after that the day is marked decided with reason `missed`.

## D15. Stand-down boundary without a Hearth schedule

Before phase 3 owns the schedule, there is no known next block, so a stand-down runs for the full
`standdown_timeout_min` (180). With the schedule switch on it ends at the next block or the timeout, whichever
is sooner.

## D16. Learning application (flag off)

When `apply_learning` is on: baseline and slope buckets add a bounded (+/- 1.0 C) correction into the adaptive
offset, which then goes through the same offset cap and band clamp as everything else; the skip bucket raises
the skip threshold by its bounded correction; the preheat bucket scales the safety factor by at most 25 %.
Corrections need 3+ decayed same-direction events and a mean pointing the same way. Nothing can escape
`band_min`/`band_max` because the writer refuses it.

## D17. CI Python

Home Assistant 2026.9.3 requires Python 3.14.2; CI uses `actions/setup-python` 3.14 with
`pytest-homeassistant-custom-component==0.13.366`. Core tests are Python 3.11+ and run without HA.

## D18. HACS validation

`hacs/action` fetches `manifest.json` and `hacs.json` anonymously from raw.githubusercontent.com, which
returns nothing for a private repository. While the repo was private the job ran with `continue-on-error`
and `ignore: brands`. Both were removed when the repo went public: the brand check is satisfied by the
local `custom_components/hearth/brand/` assets, and the topics check needs GitHub repository topics.

## D19. Licence and brand assets

MIT licence added (the spec did not name one; the HACS licence check needs a file). Simple generated brand
PNGs live in `custom_components/hearth/brand/` so HACS can show an icon without a brands-repo submission.

## D20. Skip preview is continuous

Rather than a single 21:00 computation, the preview is recomputed on every tick from the current cache: for
today until the decision time, then for tomorrow. The spec's "computed the prior evening, re-confirmed at
decision time" is a subset of that. Before the decision time, when today's forecast looks warm and the skip
switch is on, `skip_status` reads `preview`.

## D21. Setback restore rule

The eco preset is restored at `setback_restore_time` (09:00 next day). If the eco number no longer shows the
value Hearth wrote (someone edited it overnight) Hearth leaves it alone and records `left_as_is`.

## D22. Manual intervention during a skip

A preset change that Hearth's write log did not produce (within `write_match_window_s`) ends the skip with
`manual_intervention` and no restore. Every other abort restores the previous preset. Phase 4 builds
override detection on the same signal.

## D23. Schedule ownership and manual changes

With the schedule switch on, Hearth applies a block once per instance (keyed by its start time and
preset) and remembers which instance it applied. It never re-applies within the same block, so a manual
preset change stands until the next block boundary, and a restart never re-fires a block that was already
applied (or that a person has since changed). Exact timing comes from a one-shot timer armed for the next
block start or preheat start, with the 5 min tick as a safety net.

## D24. Preheat is a plan recomputed every tick

The next warm-by block within `preheat_lookahead_hours` (12) gets a plan: target = that preset's VTherm
number (minus `solar_gain_discount` on sunny mornings for solar rooms), deficit from the current indoor
temperature, outdoor from the hourly forecast an hour before warm-by (else the current reading), rate from
the learned model (or 1.0 C/h until five clean runs), lead capped at `max_preheat_min`. When now passes the
planned start, the block's preset is applied early. Preheat requires the schedule switch; the plan and
estimate are always visible in `sensor.<room>_next_block` attributes, the state string only shows
"preheat est." when preheat is enabled.

## D25. Skip and schedule interplay

While a skip is active the schedule does not apply blocks. A non-skippable block with a non-eco preset
starting after the skip began ends the skip (`non_skippable_block`) and applies. When a skip ends for any
other reason with the schedule on, the restore target is the current block's preset rather than the preset
captured at skip start.

## D26. What counts as an override, and what stands the room down

Stand-down and the ledger sit behind the `override_learning` switch (phase 4 is off until enabled); the
`hearth.override` service stands a room down regardless. A change stands the room down when the room is in
scope (not dormant) and either the preset it left is an affected preset or it switched into one. Storm and
stale-forecast exclusions apply to the ledger only: the second change of an evening still stands the room
down, it just is not recorded. Changes while dormant (window open, frost, away...) do neither.

Three change kinds are detected from the climate entity's state: a preset switch Hearth did not make
(`preset`), a preset switch to `none` with a target change (`setpoint`, VTherm's manual-temperature
behaviour), and a target change within the same preset that does not match one of Hearth's own number
writes (`temp`, a hand edit of the preset number).

## D27. Ledger magnitude for preset switches

A preset switch's magnitude is the difference between the two preset temperatures, which can be 5 C or more
and would swamp a mean correction. Ledger magnitudes are capped at twice `learning_target_bound` (2.0 C);
the raw value is kept in the stand-down sensor's `last_override` attribute.

## D28. Detection judges the new state

Override detection runs from the climate state-change listener before the controller re-evaluates, so it
snapshots the *new* state for dormancy and preset context rather than the last evaluated snapshot.

## D29. Frost is not dormancy; VTherm central frost is (supersedes the frost part of spec section 5)

The spec made any frost preset dormant. With Hearth owning the schedule that stalls it: the first frost
block Hearth applies would make the room dormant and no later block would ever apply. Agreed replacement:

1. Frost applied by Hearth's schedule is a normal state; Hearth stays active.
2. VTherm's central mode "Frost protection" (read from `specific_states.last_central_mode`) is the
   holiday switch and makes the room dormant with reason `central_frost`.
3. Frost set by hand on one room is a manual change. The schedule leaves it until the next block
   boundary (D23); with override learning on it also stands the room down. Frost is not an affected
   preset, so it is not learned from unless it replaces comfort/boost.

A skip never starts while the room is in frost (reason `in_frost`): switching frost to eco would add heat.
Eco rather than frost is the recommended resting state between comfort blocks (recovery from frost can
exceed the preheat cap, and setback depth only acts on eco); frost stays available per block.

## D30. Bank holidays via the Workday integration

Optional Tier 2 `workday_entity_id`. Each evaluation Hearth asks `workday.check_date` about yesterday to
two days ahead (cached per date in the room store). A Mon-Fri date that is not a workday uses the
schedule's `non_workday` blocks, or Saturday's when there are none. Weekends always use their own blocks.
A failed lookup is not cached and is retried next tick; today falls back to the sensor's state; anything
else unknown follows its weekday.
