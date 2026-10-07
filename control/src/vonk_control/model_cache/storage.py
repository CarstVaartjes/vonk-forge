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

from .. import model_cache_states
from ..model_cache_contract import ModelCacheObjectReceipt
from ..models import ModelCacheOperation, ModelCacheSet, ModelCacheSetArtifact
from ..storage_demands import NAS_MODELS, StorageDemands
from ..strict_json import read_stored_model
from .artifacts import ArtifactSpec, _is_hex
from .constants import _DIGEST_LENGTH, SCHEMA_VERSION
from .persistence import _manifest_of
from .source_helpers import _fsync_directory
from .views import StorageSummary

if TYPE_CHECKING:
    from .service import ModelCacheService


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

        Admission and previews need only this number. ``storage_summary`` walks
        every stored object, set and in-flight operation and is for the storage
        view, never for a read that repeats once per recipe.
        """
        cache = cast("ModelCacheService", self)

        return max(0, shutil.disk_usage(cache._root).free - cache._reserve_bytes)

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
        # Partial checkpoints are storage facts. They are measured before the
        # read transaction opens so no SQL session spans the directory scan.
        partial_bytes: dict[str, int] = {}
        partial_root = cache._root / "partials"
        for partial in partial_root.glob("*/*.part"):
            if (
                partial.is_file()
                and not partial.is_symlink()
                and len(partial.stem) == _DIGEST_LENGTH
                and _is_hex(partial.stem)
            ):
                try:
                    partial_bytes[partial.stem] = max(
                        partial_bytes.get(partial.stem, 0), partial.stat().st_size
                    )
                except OSError:
                    continue
        unique_used = sum(object_bytes.values())
        with cache._session(write=True) as session:
            sets = list(session.scalars(select(ModelCacheSet)))
            memberships = list(session.scalars(select(ModelCacheSetArtifact)))
            operations = list(
                session.scalars(
                    select(ModelCacheOperation).where(
                        ModelCacheOperation.state.in_(model_cache_states.LIVE)
                    )
                )
            )
            for row in sets:
                cache._refresh_protection(session, row)
            protected_sets = {row.artifact_set_sha256 for row in sets if row.protected}
            protected_artifacts = {
                item.artifact_sha256
                for item in memberships
                if item.artifact_set_sha256 in protected_sets
            }
            in_flight_artifacts: dict[str, int] = {}
            for operation in operations:
                if operation.kind not in {"download", "repair"}:
                    continue
                payload = cache._transfer_or_none(operation)
                if payload is None:
                    continue  # unreadable: its objects are not counted in flight
                manifest = _manifest_of(payload)
                for item in manifest.artifacts:
                    in_flight_artifacts.setdefault(item.sha256, item.expected_bytes)
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
                max(
                    0,
                    expected
                    - max(
                        object_bytes.get(digest, 0),
                        partial_bytes.get(digest, 0),
                        cache._stored_object_bytes(digest),
                    ),
                )
                for digest, expected in in_flight_artifacts.items()
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
