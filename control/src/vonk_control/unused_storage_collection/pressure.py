"""Unused storage collection: pressure concerns."""

from __future__ import annotations

import math
import os
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from vonk_agent_protocol import UnknownOutcomeError

from ..artifact_lifecycle import ArtifactLifecycleError
from ..exact_integer_storage import DecimalIntegerOrderKey
from ..logging import log_event
from ..models import InstallationNode, ModelCacheSet
from ..runtime_image_preparation import RuntimeImagePreparationError
from ..storage_demands import NAS_IMAGES, NAS_MODELS, StorageDemand, spark_scope
from .common import _LOGGER, _RECEIPT_SUFFIX, INVENTORY_MAX_AGE, _eviction_order, _Kept
from .evidence import _Evidence, _Item, _Outcome, _Pressure, _Round
from .references import (
    _code,
    _latest_snapshots,
    _model_removal_in_flight,
    _spark_settling,
)

if TYPE_CHECKING:
    from .service import UnusedStorageCollector


def _pressures(self: UnusedStorageCollector, now: datetime) -> list[_Pressure]:

    demands = self._demands.active() if self._demands is not None else []
    pressures: list[_Pressure] = []
    with self._sessions() as session:
        largest_install = (
            session.scalar(select(func.max(InstallationNode.required_bytes))) or 0
        )
        # Canonical decimal magnitude sorts by length then byte order.
        # Return one typed integer, never materialize the whole inventory.
        largest_set = (
            session.scalar(
                select(ModelCacheSet.expected_bytes)
                .order_by(
                    func.length(ModelCacheSet.expected_bytes).desc(),
                    DecimalIntegerOrderKey(ModelCacheSet.expected_bytes).desc(),
                )
                .limit(1)
            )
            or 0
        )
        snapshots = _latest_snapshots(session)
    for node_id, (free, total, observed) in sorted(snapshots.items()):
        if now - observed > INVENTORY_MAX_AGE:
            continue
        scope = spark_scope(node_id)
        pressure = self._pressure(
            scope,
            node_id,
            free,
            total,
            observed,
            largest_install,
            [demand for demand in demands if demand.scope == scope],
        )
        if pressure is not None:
            pressures.append(pressure)
    for scope, path, kinds in self._nas_filesystems():
        try:
            total, free = self._disk_usage(path)
        except OSError:
            continue
        wanted = {
            *([NAS_MODELS] if "model" in kinds else []),
            *([NAS_IMAGES] if "image" in kinds else []),
        }
        pressure = self._pressure(
            scope,
            None,
            free,
            total,
            None,
            largest_set,
            [demand for demand in demands if demand.scope in wanted],
        )
        if pressure is not None:
            pressures.append(pressure)
    return pressures


def _pressure(
    self: UnusedStorageCollector,
    scope: str,
    node_id: str | None,
    free: int,
    total: int,
    observed: datetime | None,
    largest: int,
    demands: list[StorageDemand],
) -> _Pressure | None:

    mark = min(
        max(math.ceil(total * self._low_free_fraction), largest),
        math.ceil(total * self._low_free_cap_fraction),
    )
    reserve = max(self._reserve_floor_bytes, math.ceil(total * self._reserve_fraction))
    if self._demands is not None:
        self._demands.settle(scope, free)
    wanted = max((demand.required_free_bytes for demand in demands), default=0)
    if wanted > free:
        required = max(wanted, mark if free < mark else 0)
        why = "refused: " + "; ".join(
            sorted({f"{demand.source} {demand.reason}" for demand in demands})
        )
        demanded = True
    elif free < mark:
        required = mark
        why = f"free space {free} is below the {mark}-byte low-space line"
        demanded = False
    else:
        return None
    shortfall = required - free
    return _Pressure(
        scope,
        node_id,
        free,
        total,
        observed,
        shortfall,
        shortfall + reserve,
        why,
        demanded,
    )


def _nas_filesystems(
    self: UnusedStorageCollector,
) -> list[tuple[str, Path, frozenset[str]]]:
    roots: list[tuple[Path, str]] = []
    if self._model_cache is not None and self._model_cache_root is not None:
        roots.append((self._model_cache_root, "model"))
    if self._images is not None and self._image_cache is not None:
        roots.append((self._image_cache, "image"))
    by_device: dict[int, list[tuple[Path, str]]] = defaultdict(list)
    for root, kind in roots:
        try:
            by_device[os.stat(root).st_dev].append((root, kind))
        except OSError:
            continue
    result: list[tuple[str, Path, frozenset[str]]] = []
    for members in by_device.values():
        kinds = frozenset(kind for _root, kind in members)
        scope = "nas" if len(by_device) == 1 else f"nas:{min(kinds)}"
        result.append((scope, members[0][0], kinds))
    return result


def _relieve(
    self: UnusedStorageCollector,
    pressure: _Pressure,
    evidence: _Evidence,
    deadline: float,
    kept: Counter[str],
    attempted: dict[tuple[str, str], bool],
) -> _Outcome:

    now = self._clock()
    outcome = _Outcome(pressure.scope, pressure.why, pressure.shortfall, "evicting")
    with self._sessions() as session:
        if pressure.node_id is not None and pressure.observed_at is not None:
            settling = _spark_settling(session, pressure.node_id, pressure.observed_at)
            items = self._spark_items(session, pressure.node_id, evidence, kept)
        else:
            settling = _model_removal_in_flight(session)
            items = self._nas_items(session, evidence, kept)
    outcome.freeable = sum(item.freeable for item in items)
    if settling:
        outcome.outcome = "waiting"
        return outcome
    self._check_round(pressure, now)
    paused = self._paused_until.get(pressure.scope)
    if paused is not None and now < paused:
        outcome.outcome = "paused"
        return outcome
    if pressure.demanded and outcome.freeable < pressure.shortfall:
        # Removing part of it would cost cached items and still not admit
        # the work that asked.
        outcome.outcome = "insufficient_after_eviction"
        return outcome
    ordered = _eviction_order(items)
    queued = 0
    for item in ordered:
        if queued >= pressure.need:
            break
        done = attempted.get((item.kind, item.key))
        if done is None:
            if time.monotonic() > deadline:
                kept[f"{item.kind}: sweep budget"] += 1
                continue
            done = attempted[(item.kind, item.key)] = self._remove(item, kept, outcome)
        if done:
            queued += item.freeable
    outcome.estimated_freed = queued
    if outcome.removed["image"]:
        self._reclaim_blobs()
    if queued:
        self._rounds[pressure.scope] = _Round(queued, pressure.free)
    elif pressure.demanded:
        outcome.outcome = "insufficient_after_eviction"
    else:
        outcome.outcome = "nothing_removable"
    return outcome


def _check_round(
    self: UnusedStorageCollector, pressure: _Pressure, now: datetime
) -> None:
    """Compare what the last round promised with the free space now measured."""

    round_ = self._rounds.pop(pressure.scope, None)
    if round_ is None or round_.estimated <= 0:
        return
    freed = pressure.free - round_.free_before
    if freed * 2 >= round_.estimated:
        return
    self._paused_until[pressure.scope] = now + self._cooldown
    log_event(
        _LOGGER,
        "unused_storage.ineffective",
        service="control-worker",
        scope=pressure.scope,
        estimated_freed_bytes=round_.estimated,
        freed_bytes=max(0, freed),
        paused_until=self._paused_until[pressure.scope].isoformat(),
    )


def _remove(
    self: UnusedStorageCollector, item: _Item, kept: Counter[str], outcome: _Outcome
) -> bool:
    """Remove one item; False when anything still keeps it."""

    label = "image receipt" if item.kind == "image" else item.kind
    key = (item.kind, item.key)
    retry_at = self._retry_at.get(key)
    if retry_at is not None and self._clock() < retry_at:
        kept[f"{label}: deferred until {retry_at.isoformat()}"] += 1
        return False
    self._retry_at.pop(key, None)
    done = 0
    for member in item.members:
        try:
            if item.kind == "installation":
                self._uninstall(member, item.members)
            elif item.kind == "image":
                assert self._image_cache is not None
                self._remove_receipt(
                    member, self._image_cache / f"{member}{_RECEIPT_SUFFIX}"
                )
            elif item.kind == "model-set":
                self._remove_model_set(member)
            else:
                self._remove_model(member)
        except _Kept as why:
            kept[f"{label}: {why}"] += 1
            break
        except UnknownOutcomeError as error:
            # The failed member is retained and discovered again by the
            # next sweep. Other eligible items continue in this pass.
            retry_at = self._clock() + self._scan_interval
            self._retry_at[key] = retry_at
            kept[f"{label}: deferred {_code(error)}"] += 1
            log_event(
                _LOGGER,
                "unused_storage.deferred",
                service="control-worker",
                kind=item.kind,
                record_id=member,
                code=_code(error),
                detail=str(error),
                next_attempt_at=retry_at.isoformat(),
            )
            break
        except (
            KeyError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
            SQLAlchemyError,
            ArtifactLifecycleError,
            RuntimeImagePreparationError,
        ) as error:
            kept[f"{label}: refused {_code(error)}"] += 1
            log_event(
                _LOGGER,
                "unused_storage.refused",
                service="control-worker",
                kind=item.kind,
                record_id=member,
                code=_code(error),
                detail=str(error)[:256],
            )
            break
        done += 1
        outcome.removed["model" if item.kind == "model-set" else item.kind] += 1
        log_event(
            _LOGGER,
            "unused_storage.removing",
            service="control-worker",
            kind=item.kind,
            record_id=member,
        )
    return done == len(item.members)


def _reclaim_blobs(self: UnusedStorageCollector) -> None:

    if self._image_blobs is None:
        return
    try:
        self._image_blobs.collect()
    except (OSError, RuntimeError, SQLAlchemyError) as error:
        log_event(
            _LOGGER,
            "unused_storage.refused",
            service="control-worker",
            kind="image-blobs",
            code=_code(error),
            detail=str(error)[:256],
        )
