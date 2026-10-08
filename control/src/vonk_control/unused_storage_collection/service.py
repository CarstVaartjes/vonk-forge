"""Unused storage collection: service concerns."""

from __future__ import annotations

import time
from collections import Counter, defaultdict
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import InstallationState

from ..logging import log_event
from ..models import InstallationNode, RecipeInstallation
from ..oci_image_store import IMAGE_CACHE_DIRECTORY
from ..runtime_image_preparation import FilesystemRuntimeImageStorage
from ..settings import (
    STORAGE_EVICTION_RESERVE_FLOOR_BYTES,
    STORAGE_EVICTION_RESERVE_FRACTION,
    STORAGE_INEFFECTIVE_COOLDOWN_SECONDS,
    STORAGE_LOW_FREE_CAP_FRACTION,
    STORAGE_LOW_FREE_FRACTION,
    STORAGE_SCAN_INTERVAL_SECONDS,
)
from ..storage_demands import (
    STORAGE_EVICTING,
    STORAGE_INSUFFICIENT,
    StorageDemands,
    StorageRelief,
    spark_scope,
)
from ..worker_memory_contract import WorkerMemoryComponent
from .common import (
    _HOLDING_STATES,
    _KEPT_WORDS,
    _LOGGER,
    INVENTORY_MAX_AGE,
    SWEEP_BUDGET_SECONDS,
    ImageBlobReclaimer,
    InstallationRemoval,
    Swept,
    UnusedModelRemoval,
    _disk_usage,
    _eviction_sentence,
    _freeable_in_order,
    _kept_sentence,
    _model_objects,
)
from .evidence import _Evidence, _Item, _Round
from .pressure import (
    _check_round,
    _nas_filesystems,
    _pressure,
    _pressures,
    _reclaim_blobs,
    _relieve,
    _remove,
)
from .references import (
    _installation_kept,
    _installation_last_used,
    _installation_pointing,
    _latest_snapshots,
    _spark_settling,
)
from .removal import (
    _damaged_manifest_bytes,
    _image_items,
    _model_items,
    _nas_items,
    _remove_model,
    _remove_model_set,
    _remove_receipt,
    _uninstall,
)


class UnusedStorageCollector:
    """Frees disk on a Spark or the NAS, by removing unused items, when it is short."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        clock: Callable[[], datetime],
        lifecycle: InstallationRemoval,
        image_cache_root: Path | None = None,
        model_cache_root: Path | None = None,
        model_cache: UnusedModelRemoval | None = None,
        image_blobs: ImageBlobReclaimer | None = None,
        demands: StorageDemands | None = None,
        budget_seconds: float = SWEEP_BUDGET_SECONDS,
        disk_usage: Callable[[Path], tuple[int, int]] = _disk_usage,
        low_free_fraction: float = STORAGE_LOW_FREE_FRACTION,
        low_free_cap_fraction: float = STORAGE_LOW_FREE_CAP_FRACTION,
        reserve_fraction: float = STORAGE_EVICTION_RESERVE_FRACTION,
        reserve_floor_bytes: int = STORAGE_EVICTION_RESERVE_FLOOR_BYTES,
        scan_interval: timedelta = timedelta(seconds=STORAGE_SCAN_INTERVAL_SECONDS),
        cooldown: timedelta = timedelta(seconds=STORAGE_INEFFECTIVE_COOLDOWN_SECONDS),
    ) -> None:

        self._sessions = sessions
        self._clock = clock
        self._lifecycle = lifecycle
        self._images = (
            FilesystemRuntimeImageStorage(image_cache_root)
            if image_cache_root is not None
            else None
        )
        self._image_cache = (
            image_cache_root / IMAGE_CACHE_DIRECTORY
            if image_cache_root is not None
            else None
        )
        self._model_cache_root = model_cache_root
        self._model_cache = model_cache
        self._image_blobs = image_blobs
        self._demands = demands
        self._budget_seconds = budget_seconds
        self._disk_usage = disk_usage
        self._low_free_fraction = low_free_fraction
        self._low_free_cap_fraction = low_free_cap_fraction
        self._reserve_fraction = reserve_fraction
        self._reserve_floor_bytes = reserve_floor_bytes
        self._scan_interval = scan_interval
        self._cooldown = cooldown
        self._due_at: datetime | None = None
        self._rounds: dict[str, _Round] = {}
        self._paused_until: dict[str, datetime] = {}
        self._retry_at: dict[tuple[str, str], datetime] = {}
        self._last_outcome: dict[str, str] = {}

    def memory_footprint(self) -> dict[WorkerMemoryComponent, int]:
        return {WorkerMemoryComponent.STORAGE_COLLECTION_ROUNDS: len(self._rounds)}

    def tick(self) -> bool:
        """Look at free space at most once per scan; True when anything was removed."""

        now = self._clock()
        if self._due_at is not None and now < self._due_at:
            return False
        self._due_at = now + self._scan_interval
        try:
            result = self.collect()
        except SQLAlchemyError as error:
            # A database fault proves nothing unused; try again next scan.
            log_event(
                _LOGGER,
                "unused_storage.sweep_failed",
                service="control-worker",
                code=type(error).__name__,
            )
            return False
        return bool(result.installations or result.images or result.models)

    def collect(self) -> Swept:
        """One pass: free what the short Sparks and the NAS need, and no more."""

        now = self._clock()
        deadline = time.monotonic() + self._budget_seconds
        self._retry_at = {key: due for key, due in self._retry_at.items() if now < due}
        pressures = self._pressures(now)
        if not pressures:
            self._last_outcome.clear()
            return Swept(0, 0, 0, {})
        with self._sessions() as session:
            evidence = _Evidence.read(session, now)
        kept: Counter[str] = Counter()
        # An installation on two Sparks is one removal for both of them.
        attempted: dict[tuple[str, str], bool] = {}
        outcomes = [
            self._relieve(pressure, evidence, deadline, kept, attempted)
            for pressure in pressures
        ]
        if any("sweep budget" in reason for reason in kept):
            self._due_at = now  # continue on the next worker pass
        removed: Counter[str] = Counter()
        for outcome in outcomes:
            removed.update(outcome.removed)
        # One line per pass that did something or whose answer changed, so a
        # Spark that stays short and waiting does not repeat itself every scan.
        changed = [
            outcome
            for outcome in outcomes
            if outcome.removed.total()
            or self._last_outcome.get(outcome.scope) != outcome.outcome
        ]
        self._last_outcome = {outcome.scope: outcome.outcome for outcome in outcomes}
        if changed:
            log_event(
                _LOGGER,
                "unused_storage.eviction_pass",
                service="control-worker",
                scopes=[outcome.line() for outcome in changed],
                kept=dict(kept),
            )
        return Swept(
            removed["installation"],
            removed["image"],
            removed["model"],
            dict(kept),
            tuple(outcome.line() for outcome in outcomes),
        )

    def relief_for_spark(
        self,
        node_id: str,
        required_free_bytes: int,
        *,
        source: str,
        subject: str,
        reason: str,
    ) -> StorageRelief | None:
        """Ask for free space on a Spark for work that was refused for lack of it.

        Returns what to tell the waiting work: that unused installations are
        being removed (``storage.evicting``), or that all of them together would
        not be enough (``storage.insufficient_after_eviction``). ``None`` when
        the Spark's free space cannot be read or already covers the request.
        """

        scope = spark_scope(node_id)
        if self._demands is not None:
            self._demands.request(
                scope,
                required_free_bytes,
                source=source,
                subject=subject,
                reason=reason,
            )
        now = self._clock()
        try:
            with self._sessions() as session:
                snapshot = _latest_snapshots(session, node_id).get(node_id)
                if snapshot is None or now - snapshot[2] > INVENTORY_MAX_AGE:
                    return None
                shortfall = required_free_bytes - snapshot[0]
                if shortfall <= 0:
                    return None
                evidence = _Evidence.read(session, now)
                kept_bytes: Counter[str] = Counter()
                items = self._spark_items(
                    session, node_id, evidence, Counter(), kept_bytes
                )
                settling = _spark_settling(session, node_id, snapshot[2])
        except SQLAlchemyError:
            return None
        freeable = sum(item.freeable for item in items)
        paused = self._paused_until.get(scope)
        paused = paused if paused is not None and now < paused else None
        if freeable >= shortfall and paused is None:
            code = STORAGE_EVICTING
            detail = (
                f"Needs {shortfall} more free bytes on this Spark; "
                f"{freeable} bytes of unused installations can be removed: "
                + _eviction_sentence(items, shortfall)
                + ", least recently used first"
                + (" (waiting for the last removal to finish)." if settling else ".")
            )
        else:
            code = STORAGE_INSUFFICIENT
            detail = (
                f"Needs {shortfall} more free bytes on this Spark; only "
                f"{freeable} bytes of unused installations can be removed"
                + (
                    "; the last removals freed far less than expected, so "
                    "removal is paused"
                    if paused is not None
                    else ""
                )
                + ". "
                + _kept_sentence(kept_bytes)
                + "Stop or remove something on this Spark to make room."
            )
        return StorageRelief(
            code, shortfall, freeable, detail, paused=paused is not None
        )

    @staticmethod
    def _spark_items(
        session: Session,
        node_id: str,
        evidence: _Evidence,
        kept: Counter[str],
        kept_bytes: Counter[str] | None = None,
    ) -> list[_Item]:

        rows = session.execute(
            select(
                RecipeInstallation.id,
                RecipeInstallation.state,
                RecipeInstallation.model_content_sha256,
                InstallationNode.installed_bytes,
            )
            .join(
                InstallationNode,
                InstallationNode.installation_id == RecipeInstallation.id,
            )
            .where(
                InstallationNode.node_id == node_id,
                RecipeInstallation.state.in_(_HOLDING_STATES),
            )
        ).all()
        groups: dict[str, list[tuple[str, str, int]]] = defaultdict(list)
        for installation_id, state, model, installed in rows:
            # One model's files are shared by every installation of it on a
            # Spark, so only removing all of them frees them.
            groups[model or f"installation:{installation_id}"].append(
                (installation_id, state, installed)
            )
        objects = _model_objects(
            session, [key for key in groups if not key.startswith("installation:")]
        )
        # Which groups link each shared object, kept groups included: an object
        # a group that stays still links is not freed by removing the others.
        holders: dict[str, set[str]] = defaultdict(set)
        for key in groups:
            for digest in objects.get(key, {}):
                holders[digest].add(key)
        candidates: list[_Item] = []
        for key, members in sorted(groups.items()):
            reasons = {
                installation_id: (
                    _installation_kept(session, installation_id, evidence)
                    if state == InstallationState.INSTALLED
                    else "not installed"
                )
                for installation_id, state, _installed in members
            }
            if any(reason is not None for reason in reasons.values()):
                for reason in reasons.values():
                    kept[
                        f"installation: {reason or 'model shared with one in use'}"
                    ] += 1
                if kept_bytes is not None:
                    why = next(
                        reason for reason in reasons.values() if reason is not None
                    )
                    kept_bytes[_KEPT_WORDS.get(why, why)] += max(
                        installed for _id, _state, installed in members
                    )
                continue
            installed = max(installed for _id, _state, installed in members)
            if installed <= 0:
                kept["installation: holds no bytes"] += len(members)
                continue
            shared = objects.get(key, {})
            last_used = max(
                _installation_last_used(session, installation_id)
                for installation_id, _state, _installed in members
            )
            profiles = sorted(
                {
                    name
                    for installation_id, _state, _installed in members
                    for name in _installation_pointing(
                        session, installation_id, evidence, evidence.pointers
                    )
                }
            )
            candidates.append(
                _Item(
                    "installation",
                    key,
                    tuple(sorted(installation_id for installation_id, *_ in members)),
                    last_used,
                    last_used > evidence.cutoff,
                    installed,
                    tuple(profiles),
                    tuple(sorted(shared.items())),
                    # What is not a model object (runtime files, private copies)
                    # goes with the installation alone.
                    max(0, installed - sum(shared.values())),
                )
            )
        return _freeable_in_order(candidates, holders, kept, kept_bytes)

    _pressures = _pressures
    _pressure = _pressure
    _nas_filesystems = _nas_filesystems
    _relieve = _relieve
    _check_round = _check_round
    _remove = _remove
    _reclaim_blobs = _reclaim_blobs
    _uninstall = _uninstall
    _nas_items = _nas_items
    _image_items = _image_items
    _damaged_manifest_bytes = _damaged_manifest_bytes
    _remove_receipt = _remove_receipt
    _model_items = _model_items
    _remove_model_set = _remove_model_set
    _remove_model = _remove_model
