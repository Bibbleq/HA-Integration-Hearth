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
writes (`temp`, a hand edit of the preset number). From 0.7.0 only a person's change is also learnt from (D47).

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

## D31. Scheduler entities as a room's schedule source (0.6.0)

Ben keeps his heating schedules in the HACS Scheduler component, so a room can take its schedule from those
`switch.schedule_*` entities instead of a blob stored by `hearth.set_schedule`. Three Tier 2 options, in
`_tier2_schema`, so both the config flow `settings` step and the options flow `init` step offer them:

| Option | Selector | Default |
|---|---|---|
| `scheduler_entity_ids` | entity, domain `switch`, integration `scheduler`, multiple | none (feature off) |
| `warm_by_presets` | select, multiple, frost / eco / comfort / boost | `[comfort, boost]` |
| `skippable_first_morning` | boolean | true |

Tier 3 gains `morning_cutoff: "12:00"`. Hearth reads the entities' state attributes, which carry the whole
week whether the switch is on or off (verified against scheduler-component v3.3.8, `switch.py`
`state_attributes`): `weekdays`, `timeslots` (`"HH:MM:SS - HH:MM:SS"`, or `"HH:MM:SS"` with no stop) and
`actions` (aligned 1:1 with the timeslots). The `current_slot`, `next_slot` and `next_trigger` attributes are
not used; they are None while the switch is off. The derivation is pure logic in `core/scheduler_source.py`.

## D32. Derivation rules

1. Weekday shorthands expand: `daily` is all seven days, `workday` is Mon-Fri, `weekend` is Sat+Sun.
2. Each timeslot start becomes one block at that HH:MM; the stop time is ignored (the next block ends the
   previous one). Blocks are sorted by time within a day. `"00:00:00 - 00:00:00"` and
   `"22:00:00 - 00:00:00"` need no special case: they are blocks at 00:00 and 22:00.
3. The preset comes from the aligned action, which must be `climate.set_preset_mode` with a preset in
   frost / eco / comfort / boost (the action data key may be `data` or `service_data`). Any other action makes
   the entity unusable.
4. A block whose preset is in `warm_by_presets` is a `warm_by` block, all others are `at` blocks. With
   `skippable_first_morning`, the first warm-by block of each day is `skippable` when its time is before
   `morning_cutoff` (so a day whose first warm-by block is 13:00 has no skippable block).
5. An entity whose expanded weekday set is exactly {sat, sun} (including the `weekend` shorthand) also supplies
   the `non_workday` blocks that 0.5.0 already uses for bank holidays. Without one, the existing fallback to
   Saturday applies.
6. Two entities covering the same day is a conflict. The conflicted days are left out and the derivation is
   not applied. An entity listing the same day twice (`["mon", "daily"]`) is not a conflict.
7. A day no entity covers has no blocks, so the previous day's last preset carries over (existing model).

Decided beyond the brief, all reported as `scheduler_unsupported_action` with a detail line rather than adding
repair keys: unrecognised weekday names, empty or misaligned `timeslots`/`actions`, two slots starting at the
same time (the Schedule model forbids duplicate block times), and slots that start at sunrise or sunset
(Hearth needs a fixed clock time). Consecutive slots with the same preset are kept as separate blocks, literally
one block per slot.

## D33. Precedence, live updates and keeping the last good schedule

When `scheduler_entity_ids` is non-empty the derived schedule replaces the stored `schedule` option, on entry
setup and whenever any source's state or attributes change (`async_track_state_change_event`). It is held in
memory and in the room store, never written to the config entry options: an options write would trigger the
update listener and reload the entry on every Scheduler edit.

- A source that is unavailable, unknown, missing or not yet carrying attributes leaves the last good schedule
  in force and fires a `scheduler_source_missing` diagnostic event once per outage. The last good schedule is
  also kept in the room store (keyed to the source list), so a restart while Scheduler is not up still has a
  schedule. A source that appears later is picked up on its first state event.
- A derivation that fails (`scheduler_day_conflict`, `scheduler_unsupported_action`) is not applied; the last
  good schedule stays in force and the repair explains why. With no last good schedule the room's schedule is
  empty.
- `hearth.set_schedule` on a room with sources raises `ServiceValidationError` naming the room and its
  Scheduler entities. Rooms without sources are unchanged.
- `scheduler` is added to manifest `after_dependencies` so the entities exist before Hearth parses at startup.
  Hearth copes if they do not.

## D34. Take-over and fallback

Ownership is "the room's schedule switch is on and the global `switch.hearth_active` is on", the existing
`enabled(SWITCH_SCHEDULE)`. It is reconciled on every evaluation as a transition on an in-memory flag, so
nothing is re-asserted every tick. (0.6.0 only flagged a source that was turned back on, with a repair. From 0.7.0 Hearth
switches it off again, see D48; the repair is now the fallback for a switch-off that does not stick.)

- Not owned to owned: `switch.turn_off` on every source that exists. This also runs at entry setup when the
  switch was already on, which makes the sources off idempotently after a restart.
- Owned to not owned (schedule switch off, global switch off), entry unload, and entry removal:
  `switch.turn_on` on every source. Dormancy and stand-down are temporary states the schedule already handles
  by not writing, so they never hand back.
- Hearth does not take over until it has a usable schedule (a good derivation exists). Switching the sources
  off with nothing to replace them would leave the room with no schedule.
- On unload Hearth hands back only if it took over in this session; a source the user had deliberately turned off
  while Hearth never owned the room is left alone. On entry removal it turns the sources on regardless, because
  Hearth is gone and the Scheduler schedules are the fallback. An options change reloads the entry, so it hands
  back and then takes over again.
- One service call per entity. A failed call is logged and retried at the next evaluation.
- Hearth cannot hand back when the integration files are deleted while it owns a room (documented in the README).

## D35. Shared-source guard

Before taking over, Hearth reads each source's `entities` attribute. Every `climate.*` entity in it other than
the room's own VTherm must be a Hearth room whose schedule switch is effectively on (`enabled`). Otherwise the
room does not take over, does not apply the derived schedule (its schedule is empty), and raises
`scheduler_shared_source`. If the guard starts failing after a take-over (another room is added to the schedule
in the card, or a peer room's schedule switch goes off) Hearth hands back, because the other room needs the
schedule.

- A peer Hearth room that has not finished loading (config entry not loaded, loading or unloading) is "pending":
  no take-over yet, but no repair either. Each room re-evaluates the other source-driven rooms when it finishes
  setting up and when its schedule switch changes, so the order rooms load in does not matter.
- A source whose `entities` attribute does not include the room's VTherm is logged as a warning and used anyway.
- The guard only applies while the room wants to take over. With the switch off the derived schedule is shown
  (to check it) and nothing is touched.

## D36. Repairs

Four issues via `homeassistant.helpers.issue_registry`, warning severity, not fixable, id `<key>_<entry_id>`,
strings in `strings.json` and `translations/en.json` under `issues`, created and deleted as the condition comes
and goes, and all cleared on unload and removal:

- `scheduler_day_conflict`: placeholders `room`, `entities`, `detail` (the conflicting days).
- `scheduler_shared_source`: `entities` are the sources, `detail` the other climate entities.
- `scheduler_unsupported_action`: `entities` is the unusable schedule, `detail` says why.
- `scheduler_source_enabled`: a source is still on `scheduler_enabled_check_s` (Tier 3, default 120) seconds
  after Hearth took over, or (0.7.0, D48) after it last switched a re-enabled source off. Checked by a one-shot timer and on every source state change; `entities` lists every
  offender. One issue per room, with all entity ids in the description. It clears when the source goes off or
  the room is handed back.

No repair for a missing or unavailable source: that is a diagnostic event and a log line, as elsewhere.

## D37. Next-block sensor attributes

`sensor.hearth_<room>_next_block` gains `schedule_source` (`scheduler` when sources are configured, otherwise
`explicit` when a stored schedule exists, otherwise `none`), `source_entities`, `source_blocked_reason` and
`derived_at`. `source_blocked_reason` is None or one of `scheduler_shared_source`, `scheduler_unsupported_action`
and `scheduler_day_conflict` in that priority order; `scheduler_source_enabled` does not block anything, so it
is not reported there. `derived_at` is the ISO time of the last successful derivation, and is restored from the
room store after a restart. The room diagnostics download includes a `scheduler_source` block.

## D38. Resetting the warming-rate model

`hearth.reset_learning` takes `bucket: warming_rate`, which clears the warming-rate model (all runs and the
fit), drops a heating run in progress and the cached preheat plan, and leaves the override ledger alone. Found
on the first live day: the pilot room's VTherm read its temperature from the radiator valve, the first clean run
measured about 7 C/h, and swapping in a room sensor left that run in the model with no way to remove it short
of deleting the room. An empty `bucket` keeps meaning "every ledger bucket" and does not touch the warming rate,
so existing calls behave as before and a ledger reset never costs a winter of learned preheat. No automatic reset
on a sensor change: Hearth does not see the VTherm's configuration, only its reported temperature.

## D39. What "learning" is, and the export blob

Learning is three parts, each a stable JSON shape taken straight from an existing `to_dict()`:

- `warming_rate`: the `WarmingModel` (`a`, `b`, `runs` as `[delta, rate]` pairs, `max_runs`).
- `ledger`: the override `Ledger` (every entry, every bucket's statistics, `max_entries`).
- `running_mean`: the outdoor `RunningMeanState` (`t_rm`, `t_rm_day`, `seeded_at`, `frozen`, today's and the
  completed day's accumulators, recent daily means).

Everything else is transient and deliberately left out: the write log, stand-down, skip and setback state, the
forecast cache, the heating run in progress, the preheat plan, the Scheduler-derived schedule, the settings
switches and numbers. The blob is `{"format": "hearth.learning", "version": 1, "exported_at", "hearth_version",
"room", "vtherm", "parts": {...}}`, and a part is present only if it has content (a warming model with no runs and
no fit, an empty ledger and an unseeded running mean are left out). Because each part is the model's own
`to_dict()`, the diagnostics download holds the same three shapes (`room.mechanisms.warming`, `room.ledger`,
`room.running_mean`) and can be wrapped into a blob by hand. Parsing, validation, the content tests and the merge
helpers are pure and live in `core/learning_io.py`; `mech_transfer.py` is the mixin that applies them to a room.

## D40. `hearth.export_learning`

Room selector as the other actions, plus `parts` (list; empty or missing means all three; an unknown name raises
`ServiceValidationError`). Registered with `SupportsResponse.ONLY`. The response shape depends on the call, not on
how many rooms happen to be installed, so scripts do not break when a second room is added: a call that names a
room (`entry_id` or `vtherm_entity_id`) gets that room's bare blob; a call that names none gets
`{"rooms": {"<vtherm entity id>": blob, ...}}`, even with one room. The integration writes nothing to disk. The
blob is built from copies (`to_dict()`), so editing a response cannot reach live state.

## D41. `hearth.import_learning`

Needs exactly one room. `data` is the blob (an object); `parts` defaults to every part the blob holds, and asking
for a part the blob lacks is an error. Validation happens in two passes and never half-applies: the blob's
structure (format, version, part names) is always checked, and the shape of each *selected* part is checked in
full (types, finite numbers, ISO times with an offset, bucket names, non-negative `b`, `t_rm` present with a
`seeded_at`) into fresh model objects; only when every selected part parsed are any of them swapped in. A part
that is not selected is not validated, so a good part can be taken from a blob whose other part is damaged.
Any failure is a `ServiceValidationError` that names the field.

Applying replaces the part wholesale. The warming model is clamped to the room's `max_learning_runs` (refitted
only if runs had to be dropped); after it the heating run in progress and the preheat plan are dropped, as in D38.
The running mean is replaced together with its tracker and does *not* start a new 48 h seed hold, because the
imported state carries its own `seeded_at` (which is why a running mean without one is refused: `seed_hold_active`
treats a missing `seeded_at` as "just seeded"). Importing into a room whose VTherm differs from the blob's
`vtherm` is allowed, since that is the "room rebuilt" case, but logs a warning and names the source VTherm in the
`import_learning` diagnostic event, which also names the parts. The store is saved immediately (not the 30 s
coalesced save) and the room is re-evaluated with reason `import_learning`.

## D42. Reversible reset: the `last_reset` stash

Before `hearth.reset_learning` clears anything it stores, in the room store under `last_reset`,
`{"at": ISO, "bucket": <bucket or null>, "blob": <export blob>}`. For `bucket: warming_rate` the blob holds the
`warming_rate` part; for a ledger bucket, or an empty bucket (every ledger bucket), it holds the `ledger` part.
Only the latest reset is kept. A reset that would clear nothing (an empty model or ledger, or an empty bucket) does
not overwrite an earlier stash, so a stray second click on reset cannot destroy the way back. The reset itself now
saves at once as well, so the stash is on disk before the next restart. `hearth.undo_reset` takes only a room,
raises `ServiceValidationError` when nothing is stashed, applies the stash, clears `last_reset`, saves, and emits an
`undo_reset` diagnostic.

## D43. Undoing a single-bucket reset restores that bucket, and merges

The stash for a ledger reset is the whole ledger as it was, so the blob stays a valid export that
`import_learning` accepts. Undo does not import it wholesale, though: it restores only the reset bucket (or all
four after an empty-bucket reset) and keeps everything else in the live ledger. Replacing the whole ledger would
silently throw away overrides recorded in the other buckets between the reset and the undo, which is exactly when
someone notices the reset was a mistake. The same reasoning applies to the warming rate: runs learnt since the
reset are appended to the stashed runs and the model refitted. Entries recorded in a restored bucket since the
reset are replayed onto the stashed statistics with the update the ledger uses when it records them, so the decay
and weighting stay right. When nothing was learnt since the reset the result is exactly the pre-reset state,
down to the fitted constants and the statistics. This diverges from the brief's "undo imports `last_reset.blob`"
only where data would otherwise be lost.

## D44. Persistence and visibility

`last_reset` is a new top-level key in the room store. A key that is absent (any 0.6.x store) loads as "nothing to
undo"; a stash that is damaged or does not parse as a blob is dropped with a warning. No store migration was
needed, so `STORAGE_VERSION` stays at 1. `sensor.hearth_<room>_warming_rate` and the four
`sensor.hearth_<room>_learning_<bucket>` sensors gain `last_reset_at` and `last_reset_bucket` (both None when
nothing can be undone; the blob is never put in an attribute). The diagnostics download gains `room.last_reset`
with the time, the bucket and the blob. The actions are `hearth.export_learning`, `hearth.import_learning` and
`hearth.undo_reset`, with descriptions in `services.yaml`, `strings.json` and `translations/en.json`.

Found while testing undo across a reload: unloading the last room removed the services and nothing put them
back, because a config-entry reload runs `async_setup_entry` but not `async_setup`. After any options change on a
single-room install every Hearth action disappeared until the next restart. `async_setup_entry` now calls the
idempotent `async_setup_services`, with a regression test.

## D45. Global pause: `switch.hearth_paused`

A second global switch, created beside `switch.hearth_active` by the same config entry and under the same ownership
rule (D3). Its state is saved with the other in the `hearth.global` store (`{"active": ..., "paused": ...}`; a store
without `paused` loads as off), so a restart keeps it, and it defaults to off. Turning it on or off evaluates every
room with reason `pause_switch`.

While paused every room is dormant with reason `paused` (the dormant binary sensor's `reason`, its new
`global_paused` attribute, the diagnostics' `dormant_reason` and `global_paused`). Dormancy already means no adaptive,
skip, setback, schedule or preheat writes, heating runs tainted (and so discarded), and external changes ignored
(ignored list reason `dormant`, no stand-down, no ledger); all of that is tested for `paused`.

The whole difference from `switch.hearth_active` off is that **pause does not hand back**: ownership of Scheduler
sources is decided by `enabled(SWITCH_SCHEDULE)` (schedule switch and `hearth_active`), which the pause does not touch,
so the sources stay off. Precedence: `hearth_active` off wins, the room reads `disabled` and is handed back; with
`hearth_active` on the pause wins over every VTherm-derived dormant reason (window, away...). Both switches carry a
`description` attribute saying this plainly (entity translations have no description field, so strings.json only
carries the names).

## D46. Leaving the pause goes straight back to normal

What happened on leaving dormancy before: nothing special. The schedule applies a block once (`applied_key`), a change
made by somebody since the block started stands until the next block, and a stand-down lasts its time, so a room that
left dormancy mid-block kept whatever had been set meanwhile and only the next boundary corrected it. That is right
for a window closing and is unchanged for every dormant reason except `paused`.

Leaving `paused` (the room's reason was `paused` and is now something else) sets a resume flag, and the first
evaluation in which the room is not dormant (so unpausing with a window open waits for the window) runs
`_handle_resume`: it ends any stand-down (unpausing is a fresh start, and a stand-down left over from before the
pause would otherwise defeat the point) and forgets the applied block, so the same evaluation applies the CURRENT block's
preset, for rooms whose schedule switch is on, even if a warm-up had set comfort. The adaptive comfort recompute runs in the
same evaluation under its usual rules (it writes if the target differs from Hearth's last write), and skip, setback and
preheat follow their existing rules; nothing new was invented for them.

## D47. A person's change, an automation's change

Each external change to the VTherm (the ones the override detector already treats as manual, D26) is classified by the
new state's context: **human** if `context.user_id` is not None, otherwise **automation** (automations, scripts,
Scheduler, integrations, VTherm itself after a restart). A same-preset target change is also human when the preset
number was edited by a person in the last 10 s, because VTherm moves its own target afterwards without the person's context.

- Human: exactly the behaviour of 0.6: stand-down (cause `manual_<kind>`) and a ledger entry when override learning is on.
- Automation: Hearth still stands down (cause `automation_<kind>`, same rules: next block or the timeout, and the
  stand-down still sits behind the override-learning switch as in D26), the heating run is tainted as before, but there is
  no ledger entry. The change is added to the ignored list with reason `automation`. `last_external_change` gains
  `origin` (`human` / `automation`) and the stand-down sensor exposes it as `last_origin`.
- Tier 3 `learn_from_automations: false`. True classifies everything as human (the 0.6 behaviour, including the
  `manual_` cause).
- Hearth's own writes are recognised first, by the write log, before any classification, and are neither.

## D48. A source switched back on is switched off again (supersedes the flag-only part of D34)

When a source Scheduler entity turns on while the room is taken over and `hearth_active` is on (paused or not), Hearth
calls `switch.turn_off` on it straight away and, once it is off, re-applies its own current block (unless paused or
dormant, in which case the unpause or the end of the dormancy does it). A call that fails is retried on later evaluations
(not within 5 s of the first attempt). The `scheduler_source_enabled` repair stays, for a source that is still on
`scheduler_enabled_check_s` (120 s) after the take-over or after the last re-enable.

The Scheduler fires its current slot the moment it is enabled, so the preset write races Hearth's reaction. A preset
change within `source_reenable_grace_s` (Tier 3, default 15 s) of a source coming on is attributed to the Scheduler: no
stand-down, no ledger entry, ignored with reason `scheduler_source`, `last_external_change` untouched. Both orders are
handled. Write after the switch event: the re-enable time is remembered. Write before it (or with the switch state
committed but its event not yet handled): the live source state is checked (on, and changed within the grace window); if
neither saw it, the change is handled as usual and the source event then takes it back, restoring the stand-down, the ledger,
`last_override` and `last_external_change` that the change altered, which are captured for that purpose before every
external change. A real person's change within 15 s of a re-enable can be misread as the Scheduler's; that is the price
of the window.

## D49. Observe only: a per-room switch, and which rooms start in it

`switch.hearth_<room>_observe` is a Tier 1 setting like the mechanism switches, stored in the room's `settings`
under `observe`. Its default is OFF, so a store written before 0.7.0 (which always has its `settings` key, possibly
empty) reads as live and an upgrade changes nothing: the Living Room and Office stay in control. A room created after
this release starts observing, implemented as "a room whose store has no settings yet": `_async_load` finds no store
(or one with no `settings` key), marks the room `new_room`, starts it with `{"observe": True}` and saves. No config-flow
field was added: the rule needs nothing the user has to answer, and a field would only have a second default to keep
in step. Consequences worth knowing: re-adding a removed room keeps its stored settings (it is not new), and a crash
before the first save simply starts the room observing again, which is the safe direction. The entity name is
`Observe`, so the id is `switch.hearth_<room>_observe`; `SWITCH_OBSERVE` is in `ROOM_SWITCHES`.

## D50. What observe stops and what it keeps

One choke point. `enabled(key)` (the "may act" test every writer already uses) is now false for every mechanism key
while the room is observing; the new `wants(key)` is the switch as configured. Nothing is written to the stored
switches, so switching observe off restores exactly what was set. Consequently, while observing: no adaptive write
(the comfort target is still computed and shown), no skip, setback, schedule or preheat writes, no stand-down and no
ledger entry (override handling is behind `enabled(learning)`; the change is still stored as `last_external_change`),
no applied corrections (`learned_*` use `wants`, so the shown comfort target includes the correction that would apply),
and the room never takes over Scheduler sources. `hearth.override` starts no stand-down on an observing room (it logs a
diagnostic) because a stand-down would also taint learning runs. Observing a taken-over room hands the sources back by
the existing path (`enabled(schedule)` goes false); switching observe off with the schedule switch on takes over by
the existing path, shared-source guard included. The D48 re-assert only runs for taken-over rooms, so it does not run
while observing. A room that is observing counts as not owning its schedule for the shared-source guard of its peers.
Kept running: the warming-rate learner, the outdoor running mean, the Scheduler derivation, the forecast fetches, the
skip preview, and the preheat plan and next-block sensor, which use `wants` so they show what Hearth would do.
Turning observe on while a skip or setback is active ends it through the usual "mechanism switched off" restore; that is
the one write, and it undoes Hearth's own earlier change. `observe_only` is an attribute of the next-block,
comfort-target and dormant sensors.

## D51. The learning gate, and the Scheduler's own preset change

A run is clean only if the room was not dormant, had no stand-down and saw no external or target change after the run
started (unchanged, D12). In observe mode the Scheduler's preset change is the source of the data, so the order
matters: the external change is stamped when the VTherm state changes, the run starts at the first evaluation that sees
heating on the affected preset (its start time and target are taken then), and the taint test is
`last_external_change_at > run.started_at`, strictly. A change that begins the block is therefore earlier than the run
it begins and does not taint it, and a change after the run started does. Tested in that order.

## D52. Preset numbers are only needed to write

The `no_preset_entities` dormant check no longer applies while observing, because the numbers matter only for writing,
and dormancy would taint every run. With observe off and the numbers missing the room is dormant as before and, new, a
Repair `no_preset_entities` (per entry, warning, not fixable, placeholders `room` and `vtherm`) explains that the VTherm
must untick "use central configuration" for preset temperatures, that VTherm then creates the numbers at 7 C, and that
they must be set before Hearth takes over. It clears when the numbers appear, when the room starts observing, and on
unload or removal. The `ISSUE_NO_PRESET_ENTITIES` constant is in `const.py` and joins `ALL_ISSUES` for cleanup.

## D53. Precedence

`hearth_active` off (`disabled`) beats `paused`, which beats observe. Observe is not a dormant reason: an observing
room is not dormant and keeps learning, and the VTherm-derived reasons (window, away, safety, off, unavailable, central
frost) still apply to it and still taint runs. A paused observing room reads `paused`.

## D54. A warm-up ends when the room reaches target, not when the valve goes idle

Until 0.7.1 a heating run closed on the first sample where the VTherm was not `heating`. On the live system (Tado
smart radiator thermostats under VTherm over_climate) the valve reports `idle` while it is still part-open, once its
own radiator-side reading nears its setpoint: the Main Bedroom on 2026-10-23 went `idle` 19 min 17 s into a warm-up
with the valve 41 % open and the room still rising 1 C every 8 minutes. Every warm-up in a single-valve room was
chopped into fragments under `learn_min_run_min` and discarded; only the three-valve Kitchen learnt anything.

A run now starts as before (comfort-type preset, heating, not dormant or stood down, room below target) and then
ignores the heating flag. It is counted when the room sensor comes within `learn_reach_tolerance` (0.1 C) of the
target, using the time from start to that sample. It is discarded, with a `discarded_run` diagnostic, when the block
ends first (`block_ended`: a room that never got there, such as a zone that cannot fire the boiler), when it runs
past `learn_max_run_min` (240), or on the existing taints (dormant, stand-down, manual change, target change). A
door that opens after the room has reached target no longer costs the run, because the run is already closed.

This measures what preheat needs: the time from switching to comfort until the room sensor reads target, including
any minutes the valve spends idle. `learn_min_run_min` and `learn_min_rise` still apply, so a small deficit reached
in a few minutes is not learnt from.

## D55. A VTherm restoring itself at startup is not an override

On a Home Assistant restart every VTherm reports `off`, no preset and `is_ready: false`, then restores its preset about
20 s later. The restore carries no user, so 0.7.1 read it as an automation's preset change and stood live rooms down
for 180 minutes (Living Room and Office on 2026-10-23); a room that restored to an unaffected preset escaped.

A change is now a restore when the old state was `unavailable`/`unknown` or had `is_ready` false, or when it arrives
while Home Assistant is starting or within `startup_grace_s` (180) of `homeassistant_started`. A restore is recorded in
the ignored list with reason `startup`, does not set `last_external_change` (so it does not taint a heating run) and
asks for Hearth's current block to be put back. A change with a person's context is never a restore. A config-entry
reload on a running system gets no time window, only the not-ready rule.

## D56. Entities write only what changed

Every VTherm state change (about every 20 s per VTherm) dispatches a room update, and every Hearth entity of that
room used to write its state each time: 26 writes per update, which multiplied the Tado Hijack startup event storm and
tripped Home Assistant's runaway-event guard. Each entity now fingerprints its availability, state, icon and
attributes, records the fingerprint on every write (including switch and number handlers and Home Assistant's first
write) and skips a room update that would write the same thing. `skip_preview.computed_at` changed on every
evaluation, so it now moves only when the preview's content changes.

## D57. An open window does not stop the schedule

Until 0.7.3 every dormant reason held the schedule, so a block boundary that fell while a window was open was never
applied: on 2026-10-23 the conservatory door opened at 08:04, the 08:15 eco block was skipped, and the Living Room sat
on the 07:00 comfort preset all day underneath window frost. VTherm keeps the requested preset under its window
action and returns to it when the window closes, so Hearth now applies schedule blocks (and preheat) while the only
dormant reason is `window`. Every other dormant reason (paused, disabled, away, safety, off, unavailable, central frost,
missing preset numbers) still holds the schedule, and a window still taints heating runs and stops adaptive, skip and
setback writes.

## D58. A VTherm putting its own preset temperature back is not an override

When a window closes, when a VTherm's options are reloaded, and sometimes after Hearth's own preset write, the VTherm
moves its target to the current preset's temperature with no user on the context. 0.7.2 read that as an automation
setpoint change and stood the room down to the next block boundary (the Living Room twice on 2026-10-23). A target
change on the same, non-`none` preset to exactly that preset's number value is now ignored as `vtherm_reapply`,
unless the preset number itself changed within `write_match_window_s`: a number edit (Hearth's own, or a person's,
D47) keeps its existing handling. A manual setpoint switches VTherm to preset `none`, so it is never caught here.

## D59. A bypassed window is not dormant

VTherm's window bypass (`versatile_thermostat.set_window_bypass`) heats through an open window. Hearth read
`window_state` alone and went dormant anyway. The snapshot now treats a window as open only when it is open and not
bypassed, so a future automation can bypass the conservatory door while the conservatory is warm.

