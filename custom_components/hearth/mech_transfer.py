"""Export, import and undo of learnt values (D39-D44).

What counts as learning is three parts: the warming-rate model, the override ledger and the outdoor
running mean. `hearth.export_learning` returns them as a blob in the action response, `hearth.import_learning`
validates a blob and then replaces the chosen parts, and `hearth.reset_learning` stashes what it is about to
clear in the room store (`last_reset`) so `hearth.undo_reset` can put it back. The pure parts live in
`core/learning_io.py`.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.exceptions import ServiceValidationError
from homeassistant.loader import async_get_integration
from homeassistant.util import dt as dt_util

from .const import RESET_WARMING_RATE
from .core.comfort import RunningMeanTracker
from .core.learning_io import (
    PART_LEDGER,
    PART_WARMING,
    LearningFormatError,
    ParsedLearning,
    bucket_has_content,
    build_blob,
    clamp_warming,
    ledger_has_content,
    merge_ledger,
    merge_warming,
    parse_blob,
    select_parts,
    warming_has_content,
)
from .core.override import BUCKETS

_LOGGER = logging.getLogger(__name__)


class TransferMixin:
    """Mixed into HearthRoom. Needs the learning, schedule and running-mean state of the room."""

    last_reset: dict[str, Any] | None

    def _init_transfer(self) -> None:
        self.last_reset = None

    def _load_transfer_state(self, raw: Any) -> None:
        """Read `last_reset` from the store. A 0.6.x store has none; a damaged one is dropped."""
        self.last_reset = None
        if not isinstance(raw, dict) or not isinstance(raw.get("at"), str):
            return
        try:
            parse_blob(raw.get("blob"))
        except LearningFormatError as err:
            _LOGGER.warning("%s: ignoring unreadable stashed reset: %s", self.room_name, err)
            return
        self.last_reset = {"at": raw["at"], "bucket": raw.get("bucket"), "blob": raw["blob"]}

    def last_reset_attrs(self) -> dict[str, Any]:
        reset = self.last_reset
        return {"last_reset_at": reset["at"] if reset else None, "last_reset_bucket": reset["bucket"] if reset else None}

    # ------------------------------------------------------------- export

    async def _hearth_version(self) -> str | None:
        try:
            return str((await async_get_integration(self.hass, "hearth")).version)
        except Exception:  # noqa: BLE001 - the version is informational; never fail an export over it
            return None

    async def _build_blob(self, parts: tuple[str, ...]) -> dict:
        return build_blob(
            exported_at=dt_util.utcnow().isoformat(),
            hearth_version=await self._hearth_version(),
            room=self.room_name,
            vtherm=self.vtherm.entity_id,
            parts=parts,
            warming=self.warming_model,
            ledger=self.ledger,
            running_mean=self.running_mean,
        )

    async def async_export_learning(self, parts: list[str] | None) -> dict:
        try:
            wanted = select_parts(parts)
        except LearningFormatError as err:
            raise ServiceValidationError(str(err)) from err
        return await self._build_blob(wanted)

    # ------------------------------------------------------------- import

    async def async_import_learning(self, data: Any, parts: list[str] | None) -> None:
        try:
            parsed = parse_blob(data, parts)  # validates every selected part before anything is touched
        except LearningFormatError as err:
            raise ServiceValidationError(f"Cannot import learning into {self.room_name}: {err}") from err
        note = ""
        if parsed.vtherm and parsed.vtherm != self.vtherm.entity_id:
            note = f" (exported from {parsed.vtherm})"
            _LOGGER.warning(
                "%s: importing learning exported from a different VTherm (%s, this room drives %s)",
                self.room_name,
                parsed.vtherm,
                self.vtherm.entity_id,
            )
        self._apply_learning(parsed)
        self._diagnostic("import_learning", f"Imported {', '.join(parsed.part_names)}{note}")
        await self._async_commit_learning("import_learning")

    def _apply_learning(self, parsed: ParsedLearning, *, undo: bool = False, undo_buckets: tuple[str, ...] = ()) -> None:
        """Swap the parsed parts in. Cannot fail: `parsed` is already validated."""
        if parsed.warming is not None:
            max_runs = int(self.const("max_learning_runs"))
            if undo:
                self.warming_model = merge_warming(self.warming_model, parsed.warming, max_runs)
            else:
                self.warming_model = clamp_warming(parsed.warming, max_runs)
            # The run in progress and the cached plan were measured against the old model.
            self.heating_run = None
            self.preheat_plan = None
        if parsed.ledger is not None:
            if undo:
                self.ledger = merge_ledger(self.ledger, parsed.ledger, undo_buckets, self.half_life)
            else:
                self.ledger = parsed.ledger
        if parsed.running_mean is not None:
            # The state carries its own seeded_at, so no new 48 h seed hold starts.
            self.running_mean = parsed.running_mean
            self.tracker = RunningMeanTracker(self.running_mean, alpha=float(self.const("alpha")))

    async def _async_commit_learning(self, reason: str) -> None:
        """Save now (a stash or an import must not wait for the 30 s coalesced save), then recompute."""
        await self._store.async_save(self._data_to_save())
        await self.async_evaluate(reason)

    # ------------------------------------------------------------- reset stash and undo

    async def _stash_reset(self, bucket: str | None) -> None:
        """Keep what `async_service_reset_learning` is about to clear. A reset that clears nothing keeps the old stash."""
        if bucket == RESET_WARMING_RATE:
            if not warming_has_content(self.warming_model):
                return
            parts: tuple[str, ...] = (PART_WARMING,)
        else:
            cleared = bucket_has_content(self.ledger, bucket) if bucket else ledger_has_content(self.ledger)
            if not cleared:
                return
            # The whole ledger goes in the blob, so it stays a valid export; undo restores only `bucket`.
            parts = (PART_LEDGER,)
        self.last_reset = {"at": dt_util.utcnow().isoformat(), "bucket": bucket, "blob": await self._build_blob(parts)}

    async def async_service_undo_reset(self) -> None:
        stash = self.last_reset
        if not stash:
            raise ServiceValidationError(f"{self.room_name} has no reset to undo")
        bucket = stash.get("bucket")
        try:
            parsed = parse_blob(stash["blob"])
        except LearningFormatError as err:  # loaded stashes are already checked; this guards a stash built in memory
            raise ServiceValidationError(f"The stashed reset for {self.room_name} cannot be restored: {err}") from err
        buckets = (bucket,) if bucket in BUCKETS else BUCKETS
        self._apply_learning(parsed, undo=True, undo_buckets=buckets)
        self.last_reset = None
        what = "warming rate" if bucket == RESET_WARMING_RATE else (bucket or "all ledger buckets")
        self._diagnostic("undo_reset", f"Undid the reset of {what}; restored {', '.join(parsed.part_names)}")
        await self._async_commit_learning("undo_reset")
