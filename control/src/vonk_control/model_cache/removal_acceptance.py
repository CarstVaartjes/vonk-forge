"""Removal acceptance."""

from __future__ import annotations

import fcntl
import os
import shutil
import stat
import uuid
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime
from typing import TYPE_CHECKING, cast

from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_agent_protocol import ArtifactLifecycleCode, ModelCacheCode

from ..artifact_lifecycle import (
    ArtifactIdentity,
    ArtifactLifecycleError,
    RemovalOwnerKind,
    removal_fences_match,
    reserve_removal,
)
from ..categorized_errors import InvalidValue
from ..lifecycle.evidence import Residue
from ..lifecycle.model_cache import ModelCacheAdapter
from ..model_cache_contract import (
    ModelCacheCounters,
    ModelCacheOperationPhase,
    ModelCacheOperationProgress,
    ModelCacheRemovalPayload,
    ModelCacheRetry,
)
from ..model_cache_progress import cache_progress, progress_document
from ..models import ModelCacheOperation, ModelCacheSet, ModelCacheSetArtifact
from ..strict_json import serialize_json_value
from .constants import SCHEMA_VERSION, SOURCE_POLICY
from .errors import (
    ModelCacheConflictInvalid,
    ModelCacheConflictUnknown,
    ModelCacheDeletionFenceLost,
    ModelCacheStorageRefused,
    _ArtifactWriterBusy,
)
from .persistence import (
    _model_removal_intent_digest,
    _removal_checkpoint,
    _write_operation_payload,
)
from .views import CacheOperationView, ModelCacheRemovalScope

if TYPE_CHECKING:
    from .service import ModelCacheService


class RemovalAcceptanceMixin:
    """Removal acceptance behavior of the cache service."""

    def _model_removal_by_request(
        self, request_key: str, *, actor: str, selector: str
    ) -> CacheOperationView | None:
        cache = cast("ModelCacheService", self)
        with cache._session() as session:
            operation = session.scalar(
                select(ModelCacheOperation).where(
                    ModelCacheOperation.request_key == request_key
                )
            )
            if operation is None:
                return None
            return cache._replay_model_removal(
                operation, actor=actor, selector=selector
            )

    def _replay_model_removal(
        self, operation: ModelCacheOperation, *, actor: str, selector: str
    ) -> CacheOperationView:
        cache = cast("ModelCacheService", self)
        if operation.kind != "remove" or operation.actor != actor:
            raise ModelCacheConflictInvalid(
                ModelCacheCode.REQUEST_KEY_REUSED,
                "request key was already used for another cache operation",
            )
        # A replay names its operation by the request key; an unreadable payload
        # cannot contradict it, so the stored operation is what the caller gets.
        payload = cache._payload_or_none(operation)
        if payload is not None and payload.selector != selector:
            raise ModelCacheConflictInvalid(
                ModelCacheCode.REQUEST_KEY_REUSED,
                "request key was already used for another model removal intent",
            )
        return cache._operation_view(operation)

    def _accept_model_removal(
        self,
        session: Session,
        *,
        actor: str,
        request_key: str,
        selector: str,
        model_content_sha256: str | None,
        selected_sets: Sequence[str] | None,
        review_digest: str | None = None,
        operation_id: str | None = None,
        removal_fence: str | None = None,
        gates_reserved: bool = False,
        expected_scope: ModelCacheRemovalScope | None = None,
        verify: Callable[[Session, tuple[str, ...]], None] | None = None,
    ) -> ModelCacheOperation:
        cache = cast("ModelCacheService", self)
        existing = session.scalar(
            select(ModelCacheOperation).where(
                ModelCacheOperation.request_key == request_key
            )
        )
        if existing is not None:
            cache._replay_model_removal(existing, actor=actor, selector=selector)
            return existing

        if selected_sets is None:
            selected = tuple(
                session.scalars(
                    select(ModelCacheSet.artifact_set_sha256)
                    .where(ModelCacheSet.model_content_sha256 == model_content_sha256)
                    .order_by(ModelCacheSet.artifact_set_sha256)
                )
            )
        else:
            supplied = tuple(selected_sets)
            if len(supplied) != len(set(supplied)):
                raise InvalidValue("model removal scope contains duplicate sets")
            selected = tuple(sorted(supplied))
        # Read and validate exact SQL membership before the ordered gate
        # acquisition, then re-read it after the fences are held.
        scope = cache._model_removal_scope_for_sets(session, selected)
        if expected_scope is not None and scope != expected_scope:
            raise ModelCacheConflictUnknown(
                ArtifactLifecycleCode.REFERENCE_IDENTITY_MISMATCH,
                "model removal scope changed before parent acceptance",
            )
        operation_id = operation_id or str(uuid.uuid4())
        fence = removal_fence or str(uuid.uuid4())
        now = cache._clock()
        identities = (
            *(ArtifactIdentity("model-set", digest) for digest in scope.selected_sets),
            *(
                ArtifactIdentity("model-object", digest)
                for digest in scope.delete_objects
            ),
        )
        assignments: tuple[tuple[ArtifactIdentity, RemovalOwnerKind, str, str], ...] = (
            tuple(
                (
                    identity,
                    "model-cache-operation",
                    operation_id,
                    fence,
                )
                for identity in identities
            )
        )
        try:
            if gates_reserved:
                if not removal_fences_match(session, assignments, now=now):
                    raise ModelCacheDeletionFenceLost(
                        ArtifactLifecycleCode.DELETION_FENCE_LOST,
                        "parent did not reserve every model identity for this child",
                    )
            else:
                reserve_removal(
                    session,
                    (identity for identity, _kind, _owner, _fence in assignments),
                    owner_kind="model-cache-operation",
                    owner_id=operation_id,
                    fence=fence,
                    now=now,
                )
            locked_scope = cache._model_removal_scope_for_sets(session, selected)
            if locked_scope != scope:
                raise ModelCacheConflictUnknown(
                    ArtifactLifecycleCode.REFERENCE_IDENTITY_MISMATCH,
                    "model-set membership changed while removal ownership was reserved",
                )
            # Sets still in use do not refuse the request: the accepted fence
            # stops new consumers and each destructive step waits until the
            # current owners have released the set.
        except ArtifactLifecycleError as error:
            raise ModelCacheConflictUnknown(
                error.code,
                error.detail,
                recovery="retry" if error.retryable else None,
            ) from error
        if verify is not None:
            # An unattended removal re-proves the sets unused with every gate
            # held; raising rolls the reservation back with this transaction.
            verify(session, scope.selected_sets)

        external_memberships = set(
            session.scalars(
                select(ModelCacheSetArtifact.artifact_sha256).where(
                    ModelCacheSetArtifact.artifact_set_sha256.not_in(
                        scope.selected_sets
                    )
                    if scope.selected_sets
                    else ModelCacheSetArtifact.artifact_set_sha256.is_not(None)
                )
            )
        )
        delete_objects = tuple(
            digest
            for digest in scope.selected_objects
            if digest not in external_memberships
        )
        if delete_objects != scope.delete_objects:
            raise ModelCacheConflictUnknown(
                ArtifactLifecycleCode.REFERENCE_IDENTITY_MISMATCH,
                "model object sharing changed while removal ownership was reserved",
            )
        plan = ModelCacheRemovalPayload(
            schema_version=SCHEMA_VERSION,
            source_policy=SOURCE_POLICY,
            selector=selector,
            model_content_sha256=model_content_sha256,
            operator_action="remove-model",
            review_digest=review_digest,
            removal_fence=fence,
            selected=list(scope.selected_sets),
            selected_objects=list(scope.selected_objects),
            delete_objects=list(delete_objects),
            object_index=0,
            object_pending_bytes=None,
            reclaimed_bytes=0,
            set_index=0,
            retry=ModelCacheRetry(automatic_attempts=1, operator_retries=0),
            result=None,
        )
        total_items = len(delete_objects) + len(selected)
        progress = cache._model_removal_progress(
            phase="queued",
            total_items=total_items,
            completed_items=0,
            reclaimed_bytes=0,
            current_key=None,
            previous=None,
            now=now,
        )
        stored_plan = _write_operation_payload("remove", plan)
        removal_checkpoint = _removal_checkpoint(stored_plan)
        # The plan was written one statement ago, so it reads back.
        assert not isinstance(removal_checkpoint, Residue)
        operation = ModelCacheAdapter.new_operation(
            id=operation_id,
            request_key=request_key,
            schema_version=SCHEMA_VERSION,
            kind="remove",
            attempt=1,
            artifact_set_sha256=None,
            plan_digest=_model_removal_intent_digest(
                removal_checkpoint,
                actor=actor,
                request_key=request_key,
            ),
            payload=serialize_json_value(stored_plan),
            progress=progress_document(progress),
            actor=actor,
            created_at=now,
            updated_at=now,
        )
        session.add(operation)
        session.flush()
        if not selected:
            cache._finish_model_removal_in_session(session, operation, now=now)
        return operation

    def _model_removal_progress(
        self,
        *,
        phase: ModelCacheOperationPhase,
        total_items: int,
        completed_items: int,
        reclaimed_bytes: int,
        current_key: str | None,
        previous: ModelCacheOperationProgress | None,
        now: datetime,
    ) -> ModelCacheOperationProgress:
        return cache_progress(
            ModelCacheCounters(
                phase=phase,
                completed_artifacts=completed_items,
                total_artifacts=total_items,
                downloaded_bytes=reclaimed_bytes,
                current_artifact_key=current_key,
            ),
            previous=previous,
            now=now,
        )

    @contextmanager
    def _model_storage_lock(
        self, digest: str, *, model_set: bool = False
    ) -> Iterator[None]:
        """Take one stable managed-storage lock without waiting for its owner."""
        cache = cast("ModelCacheService", self)

        ArtifactIdentity("model-set" if model_set else "model-object", digest)
        lock_root = cache._root / "locks"
        directory_fd = os.open(
            lock_root,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
        filename = f"model-set-{digest}" if model_set else digest
        try:
            descriptor = os.open(
                filename,
                os.O_CREAT
                | os.O_RDWR
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
                0o600,
                dir_fd=directory_fd,
            )
        finally:
            os.close(directory_fd)
        with os.fdopen(descriptor, "a+b") as lock_file:
            if not stat.S_ISREG(os.fstat(lock_file.fileno()).st_mode):
                raise ModelCacheStorageRefused(
                    ModelCacheCode.LOCK_UNAVAILABLE,
                    "managed-cache lock is not a regular file",
                )
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise _ArtifactWriterBusy(digest) from None
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _model_object_size(self, digest: str) -> int:
        cache = cast("ModelCacheService", self)
        objects_fd = os.open(
            cache._root / "objects",
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
        try:
            try:
                shard_fd = os.open(
                    digest[:2],
                    os.O_RDONLY
                    | getattr(os, "O_DIRECTORY", 0)
                    | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_CLOEXEC", 0),
                    dir_fd=objects_fd,
                )
            except FileNotFoundError:
                return 0
            try:
                try:
                    metadata = os.stat(digest, dir_fd=shard_fd, follow_symlinks=False)
                except FileNotFoundError:
                    return 0
                if not stat.S_ISREG(metadata.st_mode):
                    raise ModelCacheStorageRefused(
                        ModelCacheCode.REMOVAL_PATH_UNSAFE,
                        "managed model object is not a regular file",
                    )
                return metadata.st_size
            finally:
                os.close(shard_fd)
        finally:
            os.close(objects_fd)

    def _remove_model_object_files(self, digest: str) -> None:
        """Unlink one exact object and its managed receipt, then fsync its shard."""
        cache = cast("ModelCacheService", self)

        objects_fd = os.open(
            cache._root / "objects",
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
        try:
            try:
                shard_fd = os.open(
                    digest[:2],
                    os.O_RDONLY
                    | getattr(os, "O_DIRECTORY", 0)
                    | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_CLOEXEC", 0),
                    dir_fd=objects_fd,
                )
            except FileNotFoundError:
                return
            try:
                for filename in (digest, f"{digest}.receipt.json"):
                    try:
                        metadata = os.stat(
                            filename, dir_fd=shard_fd, follow_symlinks=False
                        )
                    except FileNotFoundError:
                        continue
                    if not stat.S_ISREG(metadata.st_mode):
                        raise ModelCacheStorageRefused(
                            ModelCacheCode.REMOVAL_PATH_UNSAFE,
                            "managed model object or receipt is not a regular file",
                        )
                    os.unlink(filename, dir_fd=shard_fd)
                os.fsync(shard_fd)
            finally:
                os.close(shard_fd)
        finally:
            os.close(objects_fd)

    def _remove_model_partial_set(self, set_digest: str) -> None:
        """Remove one inactive transfer checkpoint through its opened parent."""
        cache = cast("ModelCacheService", self)

        partials_fd = os.open(
            cache._root / "partials",
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
        try:
            try:
                metadata = os.stat(
                    set_digest, dir_fd=partials_fd, follow_symlinks=False
                )
            except FileNotFoundError:
                return
            if not stat.S_ISDIR(metadata.st_mode):
                raise ModelCacheStorageRefused(
                    ModelCacheCode.REMOVAL_PATH_UNSAFE,
                    "managed model partial checkpoint is not a directory",
                )
            shutil.rmtree(set_digest, dir_fd=partials_fd)
            os.fsync(partials_fd)
        finally:
            os.close(partials_fd)
