"""Unused storage collection: removal concerns."""

from __future__ import annotations

import uuid
from collections import Counter, defaultdict
from collections.abc import Collection
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_agent_protocol import ArtifactLifecycleCode

from ..artifact_lifecycle import (
    ArtifactIdentity,
    ArtifactReferenceUnverified,
    clear_removal,
    lock_reference_gates,
    release_dead_removal_nowait,
    reserve_removal,
)
from ..artifact_reference_scan import (
    model_set_reference_findings,
    runtime_image_reference_findings,
)
from ..models import ModelCacheSet
from ..oci_image_store import DAMAGED_MANIFEST_CODES, OciImageStoreError
from ..runtime_image_preparation import RuntimeImagePreparationError
from .common import _RECEIPT_SUFFIX, ACTOR, _Kept
from .evidence import _Evidence, _Item
from .references import _image_kept, _installation_kept, _model_kept, _mtime, _utc

if TYPE_CHECKING:
    from .service import UnusedStorageCollector


def _uninstall(
    self: UnusedStorageCollector, installation_id: str, group: Collection[str] = ()
) -> None:
    # The installations of one model go together: none of them keeps the
    # shared files, so the uninstall asks the Spark to reclaim them (it
    # removes only what no other installation still links).

    others = tuple(item for item in group if item != installation_id)
    plan = self._lifecycle.preview_uninstall(installation_id, also_removing=others)
    if not plan.allowed:
        code = plan.blockers[0].code if plan.blockers else "not allowed"
        raise _Kept(f"uninstall blocked ({code})")

    def still_unused(session: Session) -> None:
        reason = _installation_kept(
            session,
            installation_id,
            _Evidence.read(session, self._clock()),
        )
        if reason is not None:
            raise _Kept(reason)

    self._lifecycle.uninstall(
        installation_id,
        plan_digest=plan.plan_digest,
        actor=ACTOR,
        request_id=str(uuid.uuid4()),
        unattended_guard=still_unused,
        also_removing=others,
    )


def _nas_items(
    self: UnusedStorageCollector,
    session: Session,
    evidence: _Evidence,
    kept: Counter[str],
) -> list[_Item]:

    return [
        *self._image_items(evidence, kept),
        *self._model_items(session, evidence, kept),
    ]


def _image_items(
    self: UnusedStorageCollector, evidence: _Evidence, kept: Counter[str]
) -> list[_Item]:

    if self._images is None or self._image_cache is None:
        return []
    items: list[_Item] = []
    for path in sorted(self._image_cache.glob(f"*{_RECEIPT_SUFFIX}")):
        archive = path.name.removesuffix(_RECEIPT_SUFFIX)
        if len(archive) != 64 or any(c not in "0123456789abcdef" for c in archive):
            continue
        modified = _mtime(path)
        reason = _image_kept(archive, evidence, modified)
        if reason is not None:
            kept[f"image receipt: {reason}"] += 1
            continue
        assert modified is not None
        try:
            size = self._images.published_archive_bytes(archive)
        except (OSError, RuntimeImagePreparationError, ValueError):
            size = 0
        if size <= 0:
            # A damaged manifest is removable: its receipt names an image
            # that cannot be served, and its files go with the next sweep.
            size = self._damaged_manifest_bytes(archive)
        if size <= 0:
            kept["image receipt: holds no bytes"] += 1
            continue
        items.append(
            _Item(
                "image",
                archive,
                (archive,),
                modified,
                modified > evidence.cutoff or archive in evidence.recent_archives,
                size,
            )
        )
    return items


def _damaged_manifest_bytes(self: UnusedStorageCollector, archive: str) -> int:
    """The on-disk size of a damaged manifest, or 0 when it is not damaged."""

    assert self._images is not None
    try:
        self._images.layout.read(f"sha256:{archive}")
    except OciImageStoreError as error:
        if error.code not in DAMAGED_MANIFEST_CODES:
            return 0
        try:
            return max(
                1, self._images.layout.blob_path(f"sha256:{archive}").stat().st_size
            )
        except OSError:
            return 0
    return 0


def _remove_receipt(self: UnusedStorageCollector, archive: str, path: Path) -> None:
    """Commit an exact fence, unlink outside SQL, then release the fence.

    The publication lock is nonblocking and precedes SQL. The committed gate
    prevents new references during unlink. A crashed sweep leaves an orphan
    intent: the normal dead-owner reaper takes this same publication lock
    before releasing it, so it cannot race a live filesystem effect.
    """

    assert self._images is not None
    now = self._clock()
    identity = ArtifactIdentity("runtime-image", archive)
    owner_id = str(uuid.uuid4())
    fence = str(uuid.uuid4())
    with self._images.publication_lock(archive):
        modified = _mtime(path)
        with self._sessions.begin() as session:
            release_dead_removal_nowait(
                session, identity, owner_kind="recipe-image-job", now=now
            )
            rows = lock_reference_gates(session, (identity,), now=now)
            if any(row.removal_owner_id is not None for row in rows):
                raise _Kept("another removal owns it")
            evidence = _Evidence.read(session, now)
            reason = _image_kept(archive, evidence, modified)
            if reason is None and runtime_image_reference_findings(
                session, (archive,)
            ).get(archive):
                reason = "referenced"
            if reason is not None:
                raise _Kept(reason)
            reserve_removal(
                session,
                (identity,),
                owner_kind="recipe-image-job",
                owner_id=owner_id,
                fence=fence,
                now=now,
            )
        try:
            self._images.remove_published(archive)
            try:
                path.lstat()
            except FileNotFoundError:
                removed = True
            else:
                removed = False
            if not removed:
                raise ArtifactReferenceUnverified(
                    ArtifactLifecycleCode.REFERENCE_SCAN_FAILED,
                    "image receipt remains after its removal attempt",
                    retryable=True,
                )
        finally:
            # Synchronous unlink has ended, including an uncertain failure.
            # A new sweep re-observes actual bytes rather than inheriting a gate.
            with self._sessions.begin() as session:
                clear_removal(
                    session,
                    (identity,),
                    owner_kind="recipe-image-job",
                    owner_id=owner_id,
                    fence=fence,
                    now=self._clock(),
                )


def _model_items(
    self: UnusedStorageCollector,
    session: Session,
    evidence: _Evidence,
    kept: Counter[str],
) -> list[_Item]:

    if self._model_cache is None:
        return []
    rows = session.execute(
        select(
            ModelCacheSet.artifact_set_sha256,
            ModelCacheSet.model_content_sha256,
            ModelCacheSet.verified_bytes,
            ModelCacheSet.last_accessed_at,
            ModelCacheSet.updated_at,
        )
    ).all()
    by_model: dict[str, list[tuple[str, int, datetime, datetime]]] = defaultdict(list)
    items: list[_Item] = []
    for set_digest, model_digest, verified, accessed, updated in rows:
        if model_digest is None:
            if (
                evidence.pointers is None
                or set_digest in evidence.operations
                or model_set_reference_findings(session, (set_digest,)).get(set_digest)
            ):
                kept["model: referenced or unreadable"] += 1
                continue
            measured = self._model_cache.unused_set_bytes(set_digest)
            if measured is None:
                kept["model: bytes unknown"] += 1
                continue
            if measured > 0:
                last_used = max(_utc(accessed), _utc(updated))
                items.append(
                    _Item(
                        "model-set",
                        set_digest,
                        (set_digest,),
                        last_used,
                        last_used > evidence.cutoff,
                        measured,
                    )
                )
        else:
            by_model[model_digest].append(
                (set_digest, verified, _utc(accessed), _utc(updated))
            )
    for digest, sets in sorted(by_model.items()):
        reason = _model_kept(session, digest, evidence)
        if reason is not None:
            kept[f"model: {reason}"] += 1
            continue
        size = sum(verified for _set, verified, _a, _u in sets)
        if size <= 0:
            counts = [self._model_cache.unused_set_bytes(value[0]) for value in sets]
            if any(value is None for value in counts):
                kept["model: bytes unknown"] += 1
                continue
            size = sum(value for value in counts if value is not None)
            if size <= 0:
                continue
        last_used = max(max(accessed, updated) for _set, _v, accessed, updated in sets)
        items.append(
            _Item(
                "model",
                digest,
                (digest,),
                last_used,
                last_used > evidence.cutoff
                or any(set_digest in evidence.recent_sets for set_digest, *_ in sets),
                size,
            )
        )
    return items


def _remove_model_set(self: UnusedStorageCollector, digest: str) -> None:

    assert self._model_cache is not None

    def verify(session: Session, sets: tuple[str, ...]) -> None:
        evidence = _Evidence.read(session, self._clock())
        if evidence.pointers is None or any(
            value in evidence.operations for value in sets
        ):
            raise _Kept("references unavailable or active")
        if any(model_set_reference_findings(session, sets).values()):
            raise _Kept("referenced")

    self._model_cache.accept_unused_set_removal(
        digest, actor=ACTOR, request_key=str(uuid.uuid4()), verify=verify
    )


def _remove_model(self: UnusedStorageCollector, digest: str) -> None:

    assert self._model_cache is not None

    def verify(session: Session, sets: tuple[str, ...]) -> None:
        reason = _model_kept(session, digest, _Evidence.read(session, self._clock()))
        if reason is None and any(model_set_reference_findings(session, sets).values()):
            reason = "referenced"
        if reason is not None:
            raise _Kept(reason)

    self._model_cache.accept_unused_removal(
        digest, actor=ACTOR, request_key=str(uuid.uuid4()), verify=verify
    )
