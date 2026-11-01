"""Storage."""

from __future__ import annotations

import json
import os
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple, cast

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_agent_protocol import LifecycleState, ModelCacheBlockerCode

from .. import model_cache_states
from ..model_cache_contract import (
    CacheOperationKind,
    ModelCacheDownloadPayload,
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
    _unique_artifacts,
)
from .constants import _DIGEST_LENGTH, SCHEMA_VERSION
from .errors import ModelCacheError
from .persistence import _manifest_of
from .source_helpers import _fsync_directory
from .views import StorageSummary

if TYPE_CHECKING:
    from .service import ModelCacheService


class StorageShortfall(NamedTuple):
    """The figures behind a transfer that does not fit, from one disk read."""

    #: Free bytes the transfer needs beside the running ones.
    required: int
    #: Free bytes on the volume and the share kept free beyond them.
    free: int
    reserve: int

    def detail(self, waiting_for: str) -> str:
        return (
            f"{waiting_for}: needs {self.required} bytes free beyond the "
            f"{self.reserve}-byte reserve, {self.free} free"
        )


class TransferNeed(NamedTuple):
    """What a transfer still adds to the NAS disk."""

    #: Bytes each object has still to land.
    objects: dict[str, int]
    #: Extra disk held while it runs: a split file's largest part during
    #: assembly, or the copy a refresh or repair writes beside a verified one.
    peak: int


#: What each running download or repair still adds to the NAS, by operation id.
RunningTransfers = dict[str, TransferNeed]


def _in_flight(
    running: RunningTransfers, *, excluding: str | None = None
) -> tuple[dict[str, int], int]:
    """What the running transfers, ``excluding`` aside, still write per object,
    and the sum of their peaks. An object two transfers share lands once, so
    it counts once.
    """
    merged: dict[str, int] = {}
    peaks = 0
    for operation_id, need in running.items():
        if operation_id == excluding:
            continue
        peaks += need.peak
        for digest, remaining in need.objects.items():
            merged[digest] = max(merged.get(digest, 0), remaining)
    return merged, peaks


class StorageMixin:
    """Storage behavior of the cache service."""

    def bind_storage_demands(self, demands: StorageDemands) -> None:
        """Attach the register a download refused for lack of disk asks space in."""
        cache = cast("ModelCacheService", self)

        cache._storage_demands = demands

    def _request_storage(
        self, new_bytes: int, reason: str, reserve: int | None = None
    ) -> None:
        """Ask for the free NAS disk a refused download needs, reserve included."""
        cache = cast("ModelCacheService", self)

        if reserve is None:
            reserve = cache.reserve_of(shutil.disk_usage(cache._root).total)
        if cache._storage_demands is not None:
            cache._storage_demands.request(
                NAS_MODELS,
                new_bytes + reserve,
                source="model-download",
                subject="",
                reason=reason,
            )

    def _ranges_fit(self, reservation: int) -> bool:
        """Whether ``reservation`` fits beyond the reserve and the ranges in flight."""
        cache = cast("ModelCacheService", self)

        return cache.free_bytes() - cache._range_reserved_bytes >= reservation

    def _capacity_detail(self, waiting_for: str, required_bytes: int) -> str:
        """The figures behind a capacity wait, for the person reading it."""
        cache = cast("ModelCacheService", self)

        usage = shutil.disk_usage(cache._root)
        return StorageShortfall(
            required_bytes, usage.free, cache.reserve_of(usage.total)
        ).detail(waiting_for)

    def free_bytes(self) -> int:
        """Free NAS bytes beyond the reserve: one ``statvfs``, no object scan.

        Admission and previews need only this number and what the running
        transfers still write (:meth:`_running_transfers`). ``storage_summary`` walks
        every stored object, set and in-flight operation and is for the storage
        view, never for a read that repeats once per recipe.
        """
        cache = cast("ModelCacheService", self)

        usage = shutil.disk_usage(cache._root)
        return max(0, usage.free - cache.reserve_of(usage.total))

    def _transfer_need(
        self,
        manifest: ArtifactSetManifest,
        transfer: ModelCacheTransfer,
        *,
        replaces: bool,
        measured: bool = False,
        cached: frozenset[str] | None = None,
    ) -> TransferNeed:
        """What a transfer still adds to the NAS disk.

        An object has landed as far as its stored copy (a refresh replaces it
        in place, needing no new space) or, failing that, the transfer's ledger
        (baseline plus bytes received) as far as checkpoint files on disk back
        it: a ledger whose partial was discarded owes the whole object again.
        A ``measured`` plan was just read from disk and is taken as it is.
        No hash: one ``lstat`` per object and one listing of the set's partials.
        """
        cache = cast("ModelCacheService", self)

        on_disk: dict[str, int] = {}
        if not measured:
            try:
                with os.scandir(cache._root / "partials" / manifest.digest) as items:
                    for item in items:
                        if item.is_file(follow_symlinks=False):
                            name = item.name[:_DIGEST_LENGTH]
                            on_disk[name] = on_disk.get(name, 0) + item.stat().st_size
            except OSError:
                pass  # no checkpoint files: the ledger is not backed
        objects: dict[str, int] = {}
        split_peak = replace_peak = 0
        for digest, spec in _unique_artifacts(manifest.artifacts).items():
            entry = transfer.artifacts.get(digest)
            ledger = 0 if entry is None else entry.baseline_bytes + entry.received_bytes
            if measured:
                landed = ledger
            else:
                kept = on_disk.get(digest, 0) + sum(
                    on_disk.get(part.sha256, 0) for part in spec.parts or ()
                )
                landed = max(cache._stored_object_bytes(digest), min(ledger, kept))
            objects[digest] = max(0, spec.expected_bytes - landed)
            if (
                spec.parts is not None
                and objects[digest]
                and (cached is None or digest not in cached)
            ):
                # A split file is assembled part by part, each part deleted
                # once appended: the disk peaks at its largest part.
                split_peak = max(
                    split_peak, max(part.expected_bytes for part in spec.parts)
                )
            if replaces and ledger < spec.expected_bytes:
                # A refresh or repair writes a stored object again beside its
                # verified copy, released once the new one is published.
                replace_peak = max(replace_peak, spec.expected_bytes)
        return TransferNeed(objects, max(split_peak, replace_peak))

    def _running_transfers(self, session: Session) -> RunningTransfers:
        """What every running download and repair still adds to the NAS disk.

        One query over the running operations, so it is bounded by the transfer
        pool, never by history. The claim gate and the storage view read this
        one figure: a transfer is claimed only into the space the running ones
        leave, so parallel downloads never fill the volume between them.
        """
        cache = cast("ModelCacheService", self)

        return cache._running_needs(cache._running_payloads(session))

    def _running_payloads(
        self, session: Session
    ) -> list[tuple[str, str, ModelCacheDownloadPayload]]:
        """The readable document of every running download and repair."""
        cache = cast("ModelCacheService", self)

        found: list[tuple[str, str, ModelCacheDownloadPayload]] = []
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
            if payload is not None:  # unreadable: the claim loop rebuilds or retires it
                found.append((operation.id, operation.kind, payload))
        return found

    def _running_needs(
        self, running: list[tuple[str, str, ModelCacheDownloadPayload]]
    ) -> RunningTransfers:
        """What the running transfers still write: file reads, no session."""
        cache = cast("ModelCacheService", self)

        needs: RunningTransfers = {}
        for operation_id, kind, payload in running:
            try:
                manifest = _manifest_of(payload)
            except (TypeError, ValueError, ModelCacheError):
                continue
            needs[operation_id] = cache._transfer_need(
                manifest,
                payload.transfer,
                replaces=kind == CacheOperationKind.REPAIR or payload.force_refresh,
            )
        return needs

    def _committed_bytes(
        self,
        need: TransferNeed,
        running: RunningTransfers,
        *,
        operation_id: str | None = None,
    ) -> int:
        """Free bytes a transfer needs beside the running ones; 0 when it adds nothing."""
        others, peaks = _in_flight(running, excluding=operation_id)
        own = need.peak + sum(
            remaining
            for digest, remaining in need.objects.items()
            if digest not in others
        )
        return 0 if own == 0 else own + sum(others.values()) + peaks

    def _storage_shortfall(
        self,
        need: TransferNeed,
        running: RunningTransfers,
        *,
        operation_id: str | None = None,
    ) -> StorageShortfall | None:
        """What a transfer lacks in the NAS space the running ones leave, or None.

        Its own remaining bytes (objects a running transfer already writes
        aside) plus its peak must fit in the free disk beyond the reserve once
        every other running transfer has landed and held its peak. A transfer
        that adds nothing always fits. When one does not fit, the storage
        demand asks retention for the whole shortfall. One disk read serves the
        check, the demand and the wait's detail.
        """
        cache = cast("ModelCacheService", self)

        committed = cache._committed_bytes(need, running, operation_id=operation_id)
        if committed == 0:
            return None
        usage = shutil.disk_usage(cache._root)
        reserve = cache.reserve_of(usage.total)
        if committed <= max(0, usage.free - reserve):
            return None
        cache._request_storage(
            committed, ModelCacheBlockerCode.INSUFFICIENT_RESERVED_STORAGE, reserve
        )
        return StorageShortfall(committed, usage.free, reserve)

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
                _in_flight(cache._running_transfers(session))[0].values()
            )
        reserve = cache.reserve_of(usage.total)
        available = max(0, usage.free - reserve)
        return StorageSummary(
            total_bytes=usage.total,
            free_bytes=usage.free,
            reserve_bytes=reserve,
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
