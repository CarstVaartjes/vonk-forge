"""Storage sweep common and evidence."""

from __future__ import annotations

import logging
import shutil
from collections import Counter
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from pydantic import TypeAdapter
from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_agent_protocol import (
    InstallationState,
    LifecycleState,
    RunState,
    RunSwitchCode,
)

from .. import job_states
from ..fleet_profile_contract import FleetProfileAssignmentInput
from ..models import ModelCacheSet
from ..recipe_action_plans import StopPlan, UninstallPlan
from ..storage_demands import (
    STORAGE_EVICTING,
    STORAGE_EVICTION_TIMED_OUT,
    STORAGE_INSUFFICIENT,
)

if TYPE_CHECKING:
    from .evidence import _Item

"""Unused storage collection: common concerns."""


"""Unused non-model assets expire after 24 hours, independent of disk pressure.

The existing exact uninstall and image-removal paths revalidate live references.
Installation removal retains shared model files; the Spark store evicts those
only under pressure, least recently used first. NAS model files likewise stay
until pressure requires their fenced removal. Pressure may also evict younger
unused runtime assets to admit work. Partial installations, orphaned directories,
build workspaces and coordination history have their existing age collectors.

Nothing is removed while it is

* an installation of the loaded profile (the selected profile's recipe selector
  resolving to its revision on its Sparks), or an image or model the newest
  revision of every recipe a saved profile names;
* running, or installed for a workload that has not stopped. A failed run is
  not running, but only the Spark can prove its process gone: the removal
  first queues that run's Stop (which joins the Sparks' current workload
  intent, so it supersedes nothing) and uninstalls on a later pass, once the
  Stop recorded the run stopped;
* named by a live or recently finished operation (searched in the stored JSON,
  as the catalog revision collector does).

A removal queued on a Spark is given time to land and show in a fresh inventory
before the next one, so the Spark's own free space (not a promise about how much
a removal frees, which hard links make uncertain) decides whether more is
needed. A round that frees far less than it promised pauses eviction there for
a while. If everything that may go would still not cover a refused request,
nothing is removed and the waiting load says so
(``storage.insufficient_after_eviction``).

Node ownership and profile scope are not asset references. Only the exact
installation, run, plan or content named by live work keeps an installation;
unrelated installations can free space even while another Spark is copying.

Every removal is re-proven while the Sparks (or artifact gates) are locked, so
a load that starts after the pass looked is never raced: the uninstall takes
no new workload intent (it supersedes nothing and leaves recovery of a running
workload alone) and is refused when live work names the installation or its
content. One item that cannot be removed is kept for the next pass and never
stops the rest.
"""


_LOGGER = logging.getLogger("vonk_control.unused_storage_collection")


ACTOR = "system:storage-sweep"


SWEEP_BUDGET_SECONDS = 30.0


INVENTORY_MAX_AGE = timedelta(seconds=300)


#: Runs no load wants. A lost run may still occupy its Sparks: the collector
#: stops it first (``_stop_failed_runs``), and uninstall waits for it stopped.
_DEAD_RUNS = (RunState.STOPPED, RunState.FAILED, RunState.LOST)


_HOLDING_STATES = (
    InstallationState.INSTALLED,
    InstallationState.INSTALLING,
    InstallationState.PARTIAL,
    InstallationState.FAILED,
)


#: Holding states an unused installation is removed from. An installing one
#: is in flight, and a partial one is the incomplete-installation sweep's
#: (``attempt_residues``); a failed one is ended and only holds disk.
_COLLECTABLE_STATES = (InstallationState.INSTALLED, InstallationState.FAILED)


_FINISHED_JOBS = job_states.words(
    LifecycleState.SUCCEEDED, LifecycleState.FAILED, LifecycleState.CANCELLED
)


_RECEIPT_SUFFIX = ".receipt.json"


_ASSIGNMENTS = TypeAdapter(list[FleetProfileAssignmentInput])


class _Kept(Exception):
    """Something still uses it (or proof is missing); try again next pass."""


class _Stopping(_Kept):
    """Its failed run's Stop was queued; the removal follows once it lands."""


class UnusedModelRemoval(Protocol):
    """The model cache's unattended, fenced removal seam."""

    def accept_unused_removal(
        self,
        model_content_sha256: str,
        *,
        actor: str,
        request_key: str,
        verify: Callable[[Session, tuple[str, ...]], None],
    ) -> object: ...

    def accept_unused_set_removal(
        self,
        set_digest: str,
        *,
        actor: str,
        request_key: str,
        verify: Callable[[Session, tuple[str, ...]], None],
    ) -> object: ...

    def unused_set_bytes(self, set_digest: str) -> int | None: ...


class InstallationRemoval(Protocol):
    """The recipe lifecycle's uninstall (and the Stop of a failed run before
    it), as the collector uses it."""

    def preview_stop(self, run_id: str) -> StopPlan: ...

    def stop(
        self,
        run_id: str,
        *,
        plan_digest: str,
        actor: str,
        request_id: str,
        workload_intent_ordinal: int | None = None,
    ) -> object: ...

    def preview_uninstall(
        self,
        installation_id: str,
        *,
        also_removing: Collection[str] = (),
        preserve_model_files: bool = False,
    ) -> UninstallPlan: ...

    def uninstall(
        self,
        installation_id: str,
        *,
        plan_digest: str,
        actor: str,
        request_id: str,
        unattended_guard: Callable[[Session], None] | None = None,
        also_removing: Collection[str] = (),
        preserve_model_files: bool = False,
    ) -> object: ...


class ImageBlobReclaimer(Protocol):
    """The image store's reclaim of blobs no receipt names any more."""

    def collect(self) -> object: ...


@dataclass(frozen=True, slots=True)
class Swept:
    installations: int
    images: int
    models: int
    kept: dict[str, int]
    #: What the pass expected to free, per scope, with why it ran.
    scopes: tuple[dict[str, object], ...] = ()


_KEPT_WORDS = {
    "profile": "the loaded profile uses them",
    "running": "its workload is running",
    "live operation": "a live operation names it",
    "not installed": "not installed",
}


_STORAGE_WAIT_CODES = frozenset(
    {
        RunSwitchCode.INSUFFICIENT_DISK,
        RunSwitchCode.DISK_EVICTION_PLANNED,
        STORAGE_EVICTING,
        STORAGE_EVICTION_TIMED_OUT,
        STORAGE_INSUFFICIENT,
    }
)


def _kept_sentence(kept_bytes: Mapping[str, int]) -> str:
    """What stays on the Spark, by reason, so a refusal says why nothing goes."""

    parts = [
        f"{size} bytes of installations stay because {reason}"
        for reason, size in sorted(
            kept_bytes.items(), key=lambda item: (-item[1], item[0])
        )
        if size > 0
    ]
    return ("; ".join(parts[:2]) + ". ") if parts else ""


@dataclass(frozen=True, slots=True)
class EvictionCapacity:
    """What a Spark's unused installations could free, and what stays."""

    freeable: int
    kept: str
    items: tuple[_Item, ...]

    def plan(self, shortfall: int) -> str:
        """What would be evicted to cover ``shortfall``, in eviction order."""

        return _eviction_sentence(self.items, shortfall)


def _eviction_order(items: Iterable[_Item]) -> list[_Item]:
    """Installations no saved profile points to first, then pointed ones; the
    least recently used first within each group."""

    return sorted(
        items,
        key=lambda item: (
            bool(item.profiles),
            item.recent,
            item.last_used,
            item.kind,
            item.key,
        ),
    )


def _freeable_in_order(
    candidates: list[_Item],
    holders: Mapping[str, set[str]],
    kept: Counter[str],
    kept_bytes: Counter[str] | None,
) -> list[_Item]:
    """Give each item the bytes its removal really frees, in eviction order.

    Installations link shared model objects instead of copying them, so a
    shared object is freed only when the last group that links it goes. An item
    whose objects a group that stays still links, and that holds nothing of its
    own, frees nothing ever and is not offered. The others carry the bytes they
    free after everything ahead of them (an object two removable groups share
    counts for the later one), so the sum is what removing them all frees.
    """

    removable = {item.key for item in candidates}
    useful = []
    for item in candidates:
        reachable = item.private + sum(
            size for digest, size in item.objects if holders[digest] <= removable
        )
        if reachable <= 0:
            kept["installation: shares its files with one that stays"] += len(
                item.members
            )
            if kept_bytes is not None:
                kept_bytes["installations that stay share its model files"] += (
                    item.freeable
                )
            continue
        useful.append(item)
    remaining = {digest: set(keys) for digest, keys in holders.items()}
    ordered: list[_Item] = []
    for item in _eviction_order(useful):
        freed = item.private
        for digest, size in item.objects:
            remaining[digest].discard(item.key)
            if not remaining[digest]:
                freed += size
        ordered.append(replace(item, freeable=freed))
    return ordered


def _model_objects(
    session: Session, model_digests: Collection[str]
) -> dict[str, dict[str, int]]:
    """The model files (file digest and bytes) each model is made of, from the
    model cache's recorded manifests. A model with no manifest is absent: all
    of its installed bytes are then treated as the installation's own."""

    found: dict[str, dict[str, int]] = {}
    if not model_digests:
        return found
    for model, manifest in session.execute(
        select(ModelCacheSet.model_content_sha256, ModelCacheSet.manifest).where(
            ModelCacheSet.model_content_sha256.in_(tuple(model_digests))
        )
    ):
        artifacts = manifest.get("artifacts") if isinstance(manifest, dict) else None
        if model is None or not isinstance(artifacts, list):
            continue
        objects = found.setdefault(model, {})
        for artifact in artifacts:
            if not isinstance(artifact, dict):
                continue
            digest, size = artifact.get("sha256"), artifact.get("download_bytes")
            owner = artifact.get("model_content_sha256")
            if (
                isinstance(digest, str)
                and isinstance(size, int)
                and not isinstance(size, bool)
                and size >= 0
                and owner in (None, model)
            ):
                objects[digest] = size
    return found


def _eviction_sentence(items: Iterable[_Item], shortfall: int) -> str:
    """Name what eviction removes to free ``shortfall`` bytes: unpointed
    installations first, then the bytes taken from each saved profile."""

    unpointed = 0
    by_profile: dict[str, int] = {}
    remaining = shortfall
    for item in _eviction_order(items):
        if remaining <= 0:
            break
        remaining -= item.freeable
        if item.profiles:
            by_profile[", ".join(item.profiles)] = (
                by_profile.get(", ".join(item.profiles), 0) + item.freeable
            )
        else:
            unpointed += item.freeable
    parts = []
    if unpointed:
        parts.append(f"evicting {unpointed} bytes from unused installations")
    parts.extend(
        f"evicting {size} bytes from saved profile {names}"
        for names, size in by_profile.items()
    )
    return "; ".join(parts)


def spark_eviction_capacity(
    session: Session,
    node_id: str,
    now: datetime,
    superseded_applications: Collection[str] = (),
) -> EvictionCapacity:
    """What unused installations on a Spark could free, read-only, and what stays.

    A review asks this to plan the eviction a load needs instead of refusing it:
    the installations the collector can remove (no saved profile points to them
    first, then pointed ones, least recently used first) and a sentence naming
    what it must keep, so a refusal names what blocks. It registers no demand and
    removes nothing. The loads the reviewed one supersedes keep nothing: they
    end when it is accepted, before it waits for the eviction.
    """
    from .evidence import _Evidence
    from .service import UnusedStorageCollector

    kept_bytes: Counter[str] = Counter()
    items = UnusedStorageCollector._spark_items(
        session,
        node_id,
        _Evidence.read(session, now, superseded_applications),
        Counter(),
        kept_bytes,
    )
    return EvictionCapacity(
        sum(item.freeable for item in items), _kept_sentence(kept_bytes), tuple(items)
    )


def _disk_usage(path: Path) -> tuple[int, int]:
    usage = shutil.disk_usage(path)
    return usage.total, usage.free
