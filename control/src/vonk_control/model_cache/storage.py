"""Storage."""

from __future__ import annotations

import json
import os
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, cast

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_agent_protocol import LifecycleState, ModelCacheBlockerCode

from .. import model_cache_states
from ..model_cache_contract import (
    CacheOperationKind,
    ModelCacheObjectReceipt,
    ModelCacheTransfer,
)
from ..models import ModelCacheOperation, ModelCacheSet, ModelCacheSetArtifact
from ..storage_demands import NAS_MODELS, StorageDemands
from ..strict_json import read_stored_model
from .artifacts import (
    ArtifactSetManifest,
    ArtifactSpec,
    _is_hex,
    _split_transient_bytes,
    _unique_artifacts,
)
from .constants import _DIGEST_LENGTH, SCHEMA_VERSION
from .errors import ModelCacheError
from .persistence import _manifest_of
from .source_helpers import _fsync_directory
from .views import StorageSummary

if TYPE_CHECKING:
    from .service import ModelCacheService

#: Bytes each object of a running download or repair has still to land on the
#: NAS, by operation id.
RunningTransfers = dict[str, dict[str, int]]


def _in_flight(
    running: RunningTransfers, *, excluding: str | None = None
) -> dict[str, int]:
    """What the running transfers, ``excluding`` aside, still write per object.

    An object two transfers share lands once, so it counts once.
    """
    merged: dict[str, int] = {}
    for operation_id, objects in running.items():
        if operation_id == excluding:
            continue
        for digest, remaining in objects.items():
            merged[digest] = max(merged.get(digest, 0), remaining)
    return merged


class StorageMixin:
    """Storage behavior of the cache service."""

    def bind_storage_demands(self, demands: StorageDemands) -> None:
        """Attach the register a download refused for lack of disk asks space in."""
        cache = cast("ModelCacheService", self)

        cache._storage_demands = demands

    def _request_storage(self, new_bytes: int, reason: str) -> None:
        """Ask for the free NAS disk a refused download needs, reserve included."""
        cache = cast("ModelCacheService", self)

        if cache._storage_demands is not None:
            cache._storage_demands.request(
                NAS_MODELS,
                new_bytes + cache._reserve_bytes,
                source="model-download",
                subject="",
                reason=reason,
            )

    def free_bytes(self) -> int:
        """Free NAS bytes beyond the reserve: one ``statvfs``, no object scan.

        Admission and previews need only this number and what the running
        transfers still write (:meth:`_running_transfers`). ``storage_summary`` walks
        every stored object, set and in-flight operation and is for the storage
        view, never for a read that repeats once per recipe.
        """
        cache = cast("ModelCacheService", self)

        return max(0, shutil.disk_usage(cache._root).free - cache._reserve_bytes)

    def _remaining_bytes(
        self, manifest: ArtifactSetManifest, transfer: ModelCacheTransfer
    ) -> dict[str, int]:
        """Bytes each object of a transfer has still to land on the NAS.

        What is there already is the larger of the stored object (a refresh
        replaces it in place and needs no new space) and the transfer's own
        ledger: its planned baseline plus the bytes received since. No hash and
        no directory walk: one ``lstat`` per object.
        """
        cache = cast("ModelCacheService", self)

        remaining: dict[str, int] = {}
        for digest, spec in _unique_artifacts(manifest.artifacts).items():
            entry = transfer.artifacts.get(digest)
            landed = max(
                cache._stored_object_bytes(digest),
                0 if entry is None else entry.baseline_bytes + entry.received_bytes,
            )
            remaining[digest] = max(0, spec.expected_bytes - landed)
        return remaining

    def _running_transfers(self, session: Session) -> RunningTransfers:
        """What every running download and repair has still to write to the NAS.

        One query over the running operations, so it is bounded by the transfer
        pool, never by history. Admission and the storage view read this one
        figure: a transfer is admitted only into the space the running ones
        leave, so parallel downloads never fill the volume between them.
        """
        cache = cast("ModelCacheService", self)

        running: RunningTransfers = {}
        for operation in session.scalars(
            select(ModelCacheOperation).where(
                ModelCacheOperation.kind.in_(
                    (CacheOperationKind.DOWNLOAD, CacheOperationKind.REPAIR)
                ),
                ModelCacheOperation.state.in_(
                    model_cache_states.words(LifecycleState.RUNNING)
                ),
            )
        ):
            payload = cache._transfer_or_none(operation)
            if payload is None:
                continue  # unreadable: the claim loop rebuilds or retires it
            try:
                manifest = _manifest_of(payload)
            except (TypeError, ValueError, ModelCacheError):
                continue
            running[operation.id] = cache._remaining_bytes(manifest, payload.transfer)
        return running

    def _storage_fits(
        self,
        manifest: ArtifactSetManifest,
        transfer: ModelCacheTransfer,
        running: RunningTransfers,
        *,
        operation_id: str | None = None,
        cached: frozenset[str] | None = None,
        replaces: bool = False,
    ) -> bool:
        """Whether a transfer fits in the NAS space the running ones leave.

        Its own remaining bytes (objects a running transfer already writes
        aside) plus its transient peak must fit in the free disk beyond the
        reserve once every other running transfer has landed. When it does not,
        the storage demand asks retention for the whole shortfall.
        """
        cache = cast("ModelCacheService", self)

        others = _in_flight(running, excluding=operation_id)
        needed = sum(
            remaining
            for digest, remaining in cache._remaining_bytes(manifest, transfer).items()
            if digest not in others
        )
        # A split file is assembled part by part, each part deleted once
        # appended: the disk peaks at the file plus its largest part.
        transient = _split_transient_bytes(manifest, cached)
        if replaces:
            # A refresh or repair writes a stored object again beside its
            # verified copy, released once the new one is published: the disk
            # peaks at the largest object it has still to write.
            transient = max(
                transient,
                max(
                    (
                        spec.expected_bytes
                        for digest, spec in _unique_artifacts(
                            manifest.artifacts
                        ).items()
                        if digest not in transfer.artifacts
                        or transfer.artifacts[digest].baseline_bytes
                        + transfer.artifacts[digest].received_bytes
                        < spec.expected_bytes
                    ),
                    default=0,
                ),
            )
        needed += transient
        committed = needed + sum(others.values())
        if committed <= cache.free_bytes():
            return True
        cache._request_storage(
            committed, ModelCacheBlockerCode.INSUFFICIENT_RESERVED_STORAGE
        )
        return False

    def storage_summary(self) -> StorageSummary:
        cache = cast("ModelCacheService", self)
        usage = shutil.disk_usage(cache._root)
        object_bytes: dict[str, int] = {}
        objects = cache._root / "objects"
        for path in objects.glob("*/*"):
            if (
                path.is_file()
                and not path.is_symlink()
                and len(path.name) == _DIGEST_LENGTH
                and path.name == path.name.lower()
                and _is_hex(path.name)
                and path.parent.name == path.name[:2]
            ):
                try:
                    object_bytes[path.name] = path.stat().st_size
                except OSError:
                    continue
        unique_used = sum(object_bytes.values())
        with cache._session(write=True) as session:
            sets = list(session.scalars(select(ModelCacheSet)))
            memberships = list(session.scalars(select(ModelCacheSetArtifact)))
            for row in sets:
                cache._refresh_protection(session, row)
            protected_sets = {row.artifact_set_sha256 for row in sets if row.protected}
            protected_artifacts = {
                item.artifact_sha256
                for item in memberships
                if item.artifact_set_sha256 in protected_sets
            }
            protected_bytes = sum(
                object_bytes.get(digest, 0) for digest in protected_artifacts
            )
            # Any on-disk object that is not protected can be reclaimed.  This
            # includes orphaned files left by an interrupted atomic publish;
            # reporting physical bytes keeps capacity decisions honest.
            reclaimable_bytes = sum(
                size
                for digest, size in object_bytes.items()
                if digest not in protected_artifacts
            )
            in_flight_bytes = sum(
                _in_flight(cache._running_transfers(session)).values()
            )
        available = max(0, usage.free - cache._reserve_bytes)
        return StorageSummary(
            total_bytes=usage.total,
            free_bytes=usage.free,
            reserve_bytes=cache._reserve_bytes,
            available_bytes=available,
            unique_used_bytes=unique_used,
            in_flight_bytes=in_flight_bytes,
            protected_bytes=protected_bytes,
            reclaimable_bytes=reclaimable_bytes,
        )

    def _refresh_entry_state(self, session: Session, set_digest: str) -> None:
        cache = cast("ModelCacheService", self)
        row = session.get(ModelCacheSet, set_digest)
        if row is None:
            return
        manifest = cache._stored_manifest(row)
        if manifest is None:
            return  # unknown set: left as it is until it is re-derived
        row.verified_bytes = cache._verified_bytes(session, set_digest)
        if row.state not in {"downloading", "verifying"}:
            row.state = (
                "cached"
                if cache._manifest_coverage_complete(manifest)
                else "needs-repair"
            )

    def _object_key(self, digest: str) -> str:
        return f"objects/{digest[:2]}/{digest}"

    def _object_path(self, digest: str) -> Path:
        cache = cast("ModelCacheService", self)
        return cache._root / "objects" / digest[:2] / digest

    def _receipt_path(self, digest: str) -> Path:
        cache = cast("ModelCacheService", self)
        return cache._root / "objects" / digest[:2] / f"{digest}.receipt.json"

    def _write_object_receipt(self, spec: ArtifactSpec, verified_at: datetime) -> None:
        """Publish the managed-storage receipt that owns an object's availability.

        The receipt is written after the verified bytes are in place, and it is
        replaced atomically so a reader never sees a partial document.
        """
        cache = cast("ModelCacheService", self)

        receipt = ModelCacheObjectReceipt(
            schema_version=SCHEMA_VERSION,
            sha256=spec.sha256,
            storage_key=cache._object_key(spec.sha256),
            expected_bytes=spec.expected_bytes,
            actual_bytes=spec.expected_bytes,
            verified_at=verified_at.isoformat(),
        )
        path = cache._receipt_path(spec.sha256)
        path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with open(temporary, "w", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        receipt.model_dump(mode="json"),
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                )
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        _fsync_directory(path.parent)

    def _read_object_receipt(
        self, digest: str, expected_bytes: int
    ) -> ModelCacheObjectReceipt | None:
        """Read a receipt and confirm it still describes the object on disk.

        Missing, malformed, or mismatched receipts all mean "not available":
        admission must never infer availability from bytes alone, and a
        damaged receipt is repaired through the normal prepare/verify path
        rather than trusted.
        """
        cache = cast("ModelCacheService", self)

        path = cache._receipt_path(digest)
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, NotADirectoryError, IsADirectoryError):
            return None
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        try:
            receipt = read_stored_model(ModelCacheObjectReceipt, document)
        except ValidationError:
            return None
        if receipt.sha256 != digest or receipt.expected_bytes != expected_bytes:
            return None
        return receipt

    def _object_is_available(self, digest: str, expected_bytes: int) -> bool:
        """Whether managed storage holds a verified object with its receipt."""
        cache = cast("ModelCacheService", self)

        return cache._stored_object(digest, expected_bytes) is not None

    def _partial_path(self, set_digest: str, digest: str) -> Path:
        cache = cast("ModelCacheService", self)
        return cache._root / "partials" / set_digest / f"{digest}.part"
