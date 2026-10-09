"""Durable synchronization of the reviewed canonical recipe library."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from importlib import metadata
from typing import Literal, Protocol

from pydantic import TypeAdapter
from sqlalchemy import and_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import (
    CatalogSyncCode,
    CatalogSyncState,
    LifecycleState,
    UnknownOutcomeError,
    WaitReason,
    canonical_message,
)
from vonk_forge_contracts import document_sha256

from .catalog_queries import active_head_revision
from .catalog_revision_contract import (
    CatalogRevisionContractError,
    read_catalog_document,
)
from .catalog_service import CatalogService
from .catalog_sync_contract import (
    SEMVER_PATTERN,
    ManagedCatalogStaleRecipe,
    ManagedCatalogSyncProblem,
    ManagedCatalogSyncResult,
    ManagedCatalogWithdrawnRecipe,
)
from .models import CatalogDocument, CatalogDocumentRevision, RecipeLibrarySyncRun
from .recipe_library_types import (
    RecipeLibraryError,
    RecipeLibraryItem,
    RecipeLibrarySnapshot,
)
from .recipe_packages.contracts import _snapshot_content
from .source_bundles import SourceBundleUnknown

_LOGGER = logging.getLogger(__name__)
_MAX_RESULT_ITEMS = 256
# A running sync that has made no progress for this long is dead (crashed
# Controller, lost thread); the next sync replaces it.
_SYNC_LEASE = timedelta(minutes=10)


def _controller_marker() -> str:
    """Identify what this Controller can read, so an upgrade re-syncs the catalog."""
    parts = []
    for package in ("vonk-control", "vonk-forge-public-contracts"):
        try:
            parts.append(f"{package}={metadata.version(package)}")
        except metadata.PackageNotFoundError:
            parts.append(f"{package}=unknown")
    return ";".join(parts)[:128]


class RecipeLibrarySyncReader(Protocol):
    def list(self) -> RecipeLibrarySnapshot: ...
    def fetch(self, uri: str) -> RecipeLibraryItem: ...


class CatalogSyncError(RuntimeError):
    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail[:256]
        super().__init__(self.detail)


class CatalogSyncUnsettled(UnknownOutcomeError, CatalogSyncError):
    """The sync could not settle: another sync owns the catalog, or the library
    moved on since it was read.  Unknown, asked again.

    ``run_automatic_sync`` retries on its bounded backoff; nothing was applied.
    """

    def __init__(self, code: str, detail: str) -> None:
        CatalogSyncError.__init__(self, code, detail)
        self.typed_reason = WaitReason.OBSERVATION_UNAVAILABLE


SyncTrigger = Literal["manual", "automatic"]
_TRIGGER = TypeAdapter(SyncTrigger)


@dataclass(frozen=True, slots=True)
class CatalogSyncView:
    id: str
    request_key: str
    trigger: SyncTrigger
    state: CatalogSyncState
    repository: str
    expected_commit: str | None
    commit: str | None
    library_version: str | None
    library_updated_at: datetime | None
    total_count: int
    processed_count: int
    imported_count: int
    updated_count: int
    unchanged_count: int
    skipped_count: int
    withdrawn_count: int
    withdrawn_recipes: tuple[ManagedCatalogWithdrawnRecipe, ...]
    stale_recipes: tuple[ManagedCatalogStaleRecipe, ...]
    problems: tuple[ManagedCatalogSyncProblem, ...]
    created_at: datetime
    completed_at: datetime | None
    # The newest failure since the last completed sync, so a Controller that
    # keeps failing to read the library is visible instead of silently stale.
    last_error: CatalogSyncFailure | None = None


@dataclass(frozen=True, slots=True)
class CatalogSyncFailure:
    code: str
    detail: str
    occurred_at: datetime


class ManagedRecipeCatalogSyncService:
    """Synchronize canonical Model and Recipe revisions atomically per item."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        catalog: CatalogService,
        reader: RecipeLibrarySyncReader,
        clock: Callable[[], datetime],
        repository: str = "CarstVaartjes/vonk-forge-recipes",
    ) -> None:
        self._sessions = sessions
        self._catalog = catalog
        self._reader = reader
        self._clock = clock
        self._repository = repository

    def sync(
        self,
        *,
        request_key: str,
        trigger: str,
        actor: str,
        reviewed_snapshot: RecipeLibrarySnapshot | None = None,
    ) -> CatalogSyncView:
        self._validate_request(request_key, trigger, actor)
        reviewed_content = (
            hashlib.sha256(_snapshot_content(reviewed_snapshot)).hexdigest()
            if reviewed_snapshot is not None
            else None
        )
        existing = self._by_request_key(request_key)
        if existing is not None:
            if (existing.trigger, existing.actor) != (trigger, actor) or (
                existing.reviewed_content_sha256 != reviewed_content
            ):
                raise CatalogSyncError(
                    CatalogSyncCode.REQUEST_REUSED,
                    "request key was already used for different sync semantics",
                )
            return _view(existing)
        run = RecipeLibrarySyncRun(
            request_key=request_key,
            trigger=trigger,
            state="running",
            active_slot="managed-recipes",
            repository=self._repository,
            expected_commit=reviewed_snapshot.commit
            if reviewed_snapshot is not None
            else None,
            reviewed_content_sha256=reviewed_content,
            observed_commit=None,
            total_count=0,
            processed_count=0,
            imported_count=0,
            updated_count=0,
            current_count=0,
            conflict_count=0,
            missing_count=0,
            result=json.loads(canonical_message(_empty_result(reviewed_content))),
            error_code=None,
            error_detail=None,
            actor=actor,
            controller_marker=_controller_marker(),
            created_at=self._clock(),
            started_at=self._clock(),
            heartbeat_at=self._clock(),
            completed_at=None,
        )
        try:
            with self._sessions.begin() as session:
                active = session.scalar(
                    select(RecipeLibrarySyncRun).where(
                        RecipeLibrarySyncRun.state == "running"
                    )
                )
                if active is not None and self._expired(active):
                    _LOGGER.warning(
                        "replacing managed catalog sync %s: no progress since %s",
                        active.id,
                        active.heartbeat_at or active.started_at,
                    )
                    self._expire(active)
                    session.flush()
                    active = None
                if active is not None:
                    raise CatalogSyncUnsettled(
                        CatalogSyncCode.IN_PROGRESS,
                        f"managed catalog sync {active.id} is already running",
                    )
                session.add(run)
        except IntegrityError as error:
            replay = self._by_request_key(request_key)
            if replay is not None:
                if (replay.trigger, replay.actor) != (trigger, actor) or (
                    replay.reviewed_content_sha256 != reviewed_content
                ):
                    raise CatalogSyncError(
                        CatalogSyncCode.REQUEST_REUSED,
                        "request key was already used for different sync semantics",
                    ) from error
                return _view(replay)
            raise CatalogSyncUnsettled(
                CatalogSyncCode.IN_PROGRESS, "another managed catalog sync is running"
            ) from error
        try:
            snapshot = self._reader.list()
            if reviewed_snapshot is not None and _snapshot_content(
                snapshot
            ) != _snapshot_content(reviewed_snapshot):
                raise CatalogSyncUnsettled(
                    CatalogSyncCode.PREVIEW_CHANGED,
                    "recipe library changed since it was reviewed",
                )
            prepare = getattr(self._reader, "prepare", None)
            if callable(prepare):
                prepare(snapshot)
            if self._initialize(run.id, snapshot):
                applied = self._apply(
                    run.id,
                    snapshot,
                    actor=actor,
                    trigger=trigger,
                    reviewed_content=reviewed_content,
                )
                # A run that was replaced meanwhile (its lease lapsed) stops
                # quietly: the run that replaced it owns the catalog now.
                if applied is not None:
                    self._finish(run.id, applied)
        except UnknownOutcomeError as error:
            # No verified generation was replaced. End this attempt with its
            # typed cause and release the active slot; automatic sync or a fresh
            # request can observe again without an operator recovery action.
            self._fail(
                run.id,
                str(getattr(error, "code", CatalogSyncCode.FAILED)),
                str(getattr(error, "detail", str(error))) or type(error).__name__,
            )
            return self.get(run.id)
        except Exception as error:
            # Whatever went wrong, never leave the run "running": it would
            # block every later sync until its lease expired.
            code = str(getattr(error, "code", CatalogSyncCode.FAILED))
            detail = str(getattr(error, "detail", str(error))) or type(error).__name__
            self._fail(run.id, code, detail)
            raise
        return self.get(run.id)

    def _expired(self, run: RecipeLibrarySyncRun) -> bool:
        last = run.heartbeat_at or run.started_at
        if last.tzinfo is None:
            last = last.replace(tzinfo=UTC)
        return self._clock() - last > _SYNC_LEASE

    def _expire(self, run: RecipeLibrarySyncRun) -> None:
        failed = json.loads(canonical_message(_result(run.result)))
        failed["state"] = CatalogSyncState.FAILED.value
        run.state = "failed"
        run.active_slot = None
        run.result = json.loads(canonical_message(_result(failed)))
        run.error_code = CatalogSyncCode.LEASE_EXPIRED
        run.error_detail = "managed catalog sync made no progress and was replaced"
        run.completed_at = self._clock()

    def latest(self) -> CatalogSyncView | None:
        with self._sessions() as session:
            # Automatic read failures never observed a commit; they are
            # reported as last_error on the run that still describes the
            # applied catalog rather than replacing it.
            newest = (
                select(RecipeLibrarySyncRun)
                .order_by(
                    RecipeLibrarySyncRun.created_at.desc(),
                    RecipeLibrarySyncRun.id.desc(),
                )
                .limit(1)
            )
            row = session.scalar(
                newest.where(
                    ~and_(
                        RecipeLibrarySyncRun.trigger == "automatic",
                        RecipeLibrarySyncRun.state == "failed",
                        RecipeLibrarySyncRun.observed_commit.is_(None),
                    )
                )
            ) or session.scalar(newest)
            if row is None:
                return None
            failure = session.scalar(
                select(RecipeLibrarySyncRun)
                .where(RecipeLibrarySyncRun.state == "failed")
                .order_by(
                    RecipeLibrarySyncRun.completed_at.desc(),
                    RecipeLibrarySyncRun.id.desc(),
                )
                .limit(1)
            )
            succeeded = session.scalar(
                select(RecipeLibrarySyncRun.completed_at)
                .where(RecipeLibrarySyncRun.state == "succeeded")
                .order_by(RecipeLibrarySyncRun.completed_at.desc())
                .limit(1)
            )
            view = _view(row)
            if (
                failure is None
                or failure.completed_at is None
                or (succeeded is not None and succeeded >= failure.completed_at)
            ):
                return view
            return replace(
                view,
                last_error=CatalogSyncFailure(
                    code=failure.error_code or CatalogSyncCode.FAILED,
                    detail=failure.error_detail or "managed catalog sync failed",
                    occurred_at=failure.completed_at,
                ),
            )

    def get(self, sync_id: str) -> CatalogSyncView:
        with self._sessions() as session:
            row = session.get(RecipeLibrarySyncRun, sync_id)
            if row is None:
                raise KeyError(sync_id)
            return _view(row)

    def automatic(self) -> CatalogSyncView:
        try:
            snapshot = self._reader.list()
        except (RecipeLibraryError, CatalogSyncError, OSError) as error:
            # Reading the library failed before any sync could start; record
            # it so sync-status shows why the catalog is not advancing.
            self._record_read_failure(
                str(getattr(error, "code", CatalogSyncCode.FAILED)),
                str(getattr(error, "detail", str(error))) or type(error).__name__,
            )
            raise
        self._catalog.refresh_build_policy()
        with self._sessions() as session:
            current = session.scalar(
                select(RecipeLibrarySyncRun)
                .where(
                    RecipeLibrarySyncRun.state == "succeeded",
                    RecipeLibrarySyncRun.observed_commit == snapshot.commit,
                )
                .order_by(RecipeLibrarySyncRun.completed_at.desc())
                .limit(1)
            )
            # Partial item-level syncs remain succeeded database rows for the
            # schema-2 state constraint. Only a current result proves that the
            # commit was completely applied; partial results must be retried so
            # successful immutable imports are reused while failed entries are
            # fetched again.
            if (
                current is not None
                and _result(current.result).state == CatalogSyncState.CURRENT
                and current.controller_marker == _controller_marker()
                and self._snapshot_available(snapshot)
            ):
                return _view(current)
        return self.sync(
            request_key=str(uuid.uuid4()),
            trigger="automatic",
            actor="system:recipe-library-sync",
            reviewed_snapshot=snapshot,
        )

    def _snapshot_available(self, snapshot: RecipeLibrarySnapshot) -> bool:
        """Observe exact current heads, rather than historical publication success."""
        local = self._catalog.recipe_catalog_local_revisions(
            [(item.publisher, item.slug) for item in snapshot.items]
        )
        for item in snapshot.items:
            row = local.get((item.publisher, item.slug))
            if row is None or row.content_sha256 != item.content_sha256:
                return False
            if (
                item.package_sha256 is not None
                and row.package_sha256 != item.package_sha256
            ):
                return False
            if (
                item.source_bundle_sha256 is not None
                and row.source_bundle_sha256 != item.source_bundle_sha256
            ):
                return False
        with self._sessions() as session:
            for document in snapshot.catalog_entities:
                digest = document_sha256(document)
                row = session.scalar(
                    select(CatalogDocumentRevision)
                    .join(
                        CatalogDocument,
                        CatalogDocument.id == CatalogDocumentRevision.document_id,
                    )
                    .where(
                        CatalogDocumentRevision.content_digest == digest,
                    )
                )
                if row is None:
                    return False
                selected = session.scalar(
                    select(CatalogDocumentRevision).where(
                        CatalogDocumentRevision.document_id == row.document_id,
                        active_head_revision(),
                    )
                )
                if selected is None:
                    return False
                try:
                    read_catalog_document(row)
                except CatalogRevisionContractError:
                    return False
        return not snapshot.problems

    def _record_read_failure(self, code: str, detail: str) -> None:
        """Persist one automatic read failure; a repeat only refreshes its time."""
        code, detail = code[:128], detail[:256]
        now = self._clock()
        with self._sessions.begin() as session:
            latest = session.scalar(
                select(RecipeLibrarySyncRun)
                .order_by(
                    RecipeLibrarySyncRun.created_at.desc(),
                    RecipeLibrarySyncRun.id.desc(),
                )
                .limit(1)
            )
            if (
                latest is not None
                and latest.state == "failed"
                and latest.trigger == "automatic"
                and latest.observed_commit is None
                and (latest.error_code, latest.error_detail) == (code, detail)
            ):
                latest.completed_at = now
                return
            failed = _empty_result()
            failed.state = CatalogSyncState.FAILED
            failed.problems = [
                ManagedCatalogSyncProblem(recipe_uri=None, code=code, detail=detail)
            ]
            session.add(
                RecipeLibrarySyncRun(
                    request_key=str(uuid.uuid4()),
                    trigger="automatic",
                    state="failed",
                    active_slot=None,
                    repository=self._repository,
                    expected_commit=None,
                    observed_commit=None,
                    total_count=0,
                    processed_count=0,
                    imported_count=0,
                    updated_count=0,
                    current_count=0,
                    conflict_count=0,
                    missing_count=0,
                    result=json.loads(canonical_message(failed)),
                    error_code=code,
                    error_detail=detail,
                    actor="system:recipe-library-sync",
                    created_at=now,
                    started_at=now,
                    completed_at=now,
                )
            )

    def _apply(
        self,
        run_id: str,
        snapshot: RecipeLibrarySnapshot,
        *,
        actor: str,
        trigger: str = "automatic",
        reviewed_content: str | None = None,
    ) -> ManagedCatalogSyncResult | None:
        """Apply the snapshot; ``None`` when this run was replaced meanwhile."""

        result = _empty_result(reviewed_content)
        self._catalog.refresh_build_policy()
        # Index documents the reader could not validate were already skipped;
        # report each one without holding up the rest of the snapshot.
        for problem in snapshot.problems:
            self._record_problem_values(
                result, uri=problem.recipe_uri, code=problem.code, detail=problem.detail
            )
            if not self._progress(run_id, result):
                return None
        # Catalog index entries are independent immutable documents. Import each
        # in its own transaction so one malformed model cannot hold up recipes
        # and models that are otherwise ready to apply.
        for model_document in snapshot.catalog_entities:
            try:
                self._catalog.import_catalog_models(actor, [model_document])
            except Exception as error:  # noqa: BLE001 - isolate document failures; cancellation still propagates
                self._record_problem_values(
                    result,
                    uri=None,
                    code=str(getattr(error, "code", CatalogSyncCode.MODEL_FAILED)),
                    detail=str(getattr(error, "detail", str(error))),
                )
            if not self._progress(run_id, result):
                return None
        local = self._catalog.recipe_catalog_local_revisions(
            [(item.publisher, item.slug) for item in snapshot.items]
        )
        for item in snapshot.items:
            previous = local.get((item.publisher, item.slug))
            if previous is not None and (previous.publisher, previous.slug) != (
                item.publisher,
                item.slug,
            ):
                self._record_problem(
                    result,
                    item,
                    CatalogSyncCode.IDENTITY_CHANGED,
                    "canonical recipe identity changed",
                )
            elif (
                previous is not None
                and previous.content_sha256 == item.content_sha256
                # A recipe's content digest does not cover its build source:
                # a package republished with a repaired Dockerfile keeps the
                # same recipe document.  Re-import until the stored package is
                # the published one, so the Controller never keeps a stale
                # source bundle.  A reader that publishes no package digest has
                # nothing to compare; a stored row without one is re-imported.
                and (
                    item.package_sha256 is None
                    or previous.package_sha256 == item.package_sha256
                )
                and (
                    item.source_bundle_sha256 is None
                    or previous.source_bundle_sha256 == item.source_bundle_sha256
                )
            ):
                result.unchanged_count += 1
            else:
                try:
                    hydrated = self._reader.fetch(item.uri)
                    if (
                        hydrated.content_sha256 != item.content_sha256
                        or (
                            item.package_sha256 is not None
                            and hydrated.package_sha256 != item.package_sha256
                        )
                        or (
                            item.source_bundle_sha256 is not None
                            and hydrated.source_bundle_sha256
                            != item.source_bundle_sha256
                        )
                        or hydrated.prebuilt_image != item.prebuilt_image
                    ):
                        # The library moved on while this snapshot was applied:
                        # the recipe is left as it is, named as a problem, and
                        # the next sync reads the newer library.
                        self._record_problem(
                            result,
                            item,
                            CatalogSyncCode.REVISION_CHANGED,
                            "recipe changed while the exact library snapshot was applied",
                        )
                        if not self._progress(run_id, result):
                            return None
                        continue
                    self._store_source_bundle(hydrated, actor)
                    self._catalog.import_recipe_library(
                        actor,
                        library_commit=hydrated.library_commit,
                        source_path=hydrated.source_path,
                        document=hydrated.document,
                        expected_content_sha256=hydrated.content_sha256,
                        dependency_documents=hydrated.dependencies,
                        release_version=hydrated.release.version
                        if hydrated.release
                        else None,
                        release_released_at=hydrated.release.released_at
                        if hydrated.release
                        else None,
                        package_handle=getattr(hydrated, "package_handle", None),
                        package_sha256=getattr(hydrated, "package_sha256", None),
                        source_bundle_sha256=getattr(
                            hydrated, "source_bundle_sha256", None
                        ),
                    )
                    if previous is None:
                        result.imported_count += 1
                    else:
                        result.updated_count += 1
                except SourceBundleUnknown as error:
                    # Keep the previous revision; the automatic sync retries
                    # this exact source ingress on its next bounded pass.
                    self._record_problem(result, item, error.code, error.detail)
                except Exception as error:  # noqa: BLE001 - isolate untyped fetch failures per recipe
                    self._record_problem(
                        result,
                        item,
                        str(getattr(error, "code", CatalogSyncCode.ITEM_FAILED)),
                        str(getattr(error, "detail", str(error))),
                    )
            if not self._progress(run_id, result):
                return None
        # Newest only: a recipe the published library no longer lists stops
        # being offered.  Installed and running revisions are untouched.  A
        # snapshot with skipped documents or no recipes may be incomplete, so
        # it retracts nothing; the next complete sync does.  Only the
        # Controller's own automatic sync of the published library retracts: a
        # manual or fixture sync (an operator import, the Spark canary) carries
        # a deliberately partial view and must never withdraw the rest.
        if trigger == "automatic" and snapshot.items and not snapshot.problems:
            retracted = self._catalog.retract_recipes_absent_from(
                [(item.publisher, item.slug) for item in snapshot.items]
            )
            for revision in retracted:
                # The catalog accepts looser release labels than the result
                # contract's semantic versions; the label is informative, so
                # one outside the contract is left out, never stored.
                release_version = (
                    revision.release_version
                    if revision.release_version is not None
                    and re.fullmatch(SEMVER_PATTERN, revision.release_version)
                    else None
                )
                result.withdrawn_recipes.append(
                    ManagedCatalogWithdrawnRecipe(
                        recipe_id=revision.recipe_id, release_version=release_version
                    )
                )
            result.withdrawn_count = len(retracted)
        # Prebuilt images can arrive after their revision (CI publishes the
        # bundle first and adds image digests once the builds finish), so
        # every sync records them for unchanged revisions too.
        record_prebuilt = getattr(self._catalog, "record_prebuilt_images", None)
        if callable(record_prebuilt):
            try:
                record_prebuilt(
                    {
                        (item.publisher, item.slug, item.content_sha256): (
                            item.prebuilt_image
                        )
                        for item in snapshot.items
                    }
                )
            except Exception as error:  # noqa: BLE001 - images are optional; recipes still apply
                self._record_problem_values(
                    result,
                    uri=None,
                    code=CatalogSyncCode.PREBUILT_IMAGES_FAILED,
                    detail=str(error)[:256] or type(error).__name__,
                )
        result.state = (
            CatalogSyncState.PARTIAL if result.problems else CatalogSyncState.CURRENT
        )
        return result

    def _store_source_bundle(self, item: RecipeLibraryItem, actor: str) -> None:
        source_bundle = getattr(item, "source_bundle", None)
        source_digest = getattr(item, "source_bundle_sha256", None)
        if source_bundle is not None and isinstance(source_digest, str):
            self._catalog.store_source_bundle(
                source_digest, io.BytesIO(source_bundle), actor
            )

    def _record_problem(
        self,
        result: ManagedCatalogSyncResult,
        item: RecipeLibraryItem,
        code: str,
        detail: str,
    ) -> None:
        self._record_problem_values(result, uri=item.uri, code=code, detail=detail)

    def _record_problem_values(
        self,
        result: ManagedCatalogSyncResult,
        *,
        uri: str | None,
        code: str,
        detail: str,
    ) -> None:
        result.skipped_count += 1
        if len(result.problems) < _MAX_RESULT_ITEMS:
            result.problems.append(
                ManagedCatalogSyncProblem(
                    recipe_uri=uri, code=code[:128], detail=detail[:256]
                )
            )

    def _initialize(self, run_id: str, snapshot: RecipeLibrarySnapshot) -> bool:
        """Record the snapshot; ``False`` when this run was replaced meanwhile."""

        with self._sessions.begin() as session:
            run = session.get(RecipeLibrarySyncRun, run_id)
            if run is None or run.state != "running":
                return False
            run.heartbeat_at = self._clock()
            run.observed_commit = snapshot.commit
            run.library_version = snapshot.version
            run.library_updated_at = snapshot.updated_at
            run.total_count = (
                len(snapshot.items)
                + len(snapshot.catalog_entities)
                + len(snapshot.problems)
            )
        return True

    def _progress(self, run_id: str, result: ManagedCatalogSyncResult) -> bool:
        """Record one step; ``False`` when this run was replaced meanwhile."""

        with self._sessions.begin() as session:
            run = session.get(RecipeLibrarySyncRun, run_id)
            if run is None or run.state != "running":
                return False
            parsed = result
            run.heartbeat_at = self._clock()
            run.processed_count += 1
            run.imported_count = parsed.imported_count
            run.updated_count = parsed.updated_count
            run.current_count = parsed.unchanged_count
            run.conflict_count = parsed.skipped_count
            run.result = json.loads(canonical_message(parsed))
        return True

    def _finish(self, run_id: str, result: ManagedCatalogSyncResult) -> None:
        """Settle the run as succeeded; a run replaced meanwhile stays as it is."""

        with self._sessions.begin() as session:
            run = session.get(RecipeLibrarySyncRun, run_id)
            if run is None or run.state != "running":
                return
            run.state = "succeeded"
            run.active_slot = None
            run.result = json.loads(canonical_message(result))
            run.missing_count = 0
            run.completed_at = self._clock()

    def _fail(self, run_id: str, code: str, detail: str) -> None:
        with self._sessions.begin() as session:
            run = session.get(RecipeLibrarySyncRun, run_id)
            if run is None or run.state != "running":
                return
            failed = _result(run.result)
            if len(failed.problems) < _MAX_RESULT_ITEMS:
                failed.problems.append(
                    ManagedCatalogSyncProblem(
                        recipe_uri=None, code=code[:128], detail=detail[:256]
                    )
                )
            failed.state = CatalogSyncState.FAILED
            run.state = "failed"
            run.active_slot = None
            run.result = json.loads(canonical_message(failed))
            run.error_code = code[:128]
            run.error_detail = detail[:256]
            run.completed_at = self._clock()

    def _by_request_key(self, request_key: str) -> RecipeLibrarySyncRun | None:
        with self._sessions() as session:
            row = session.scalar(
                select(RecipeLibrarySyncRun).where(
                    RecipeLibrarySyncRun.request_key == request_key
                )
            )
            if row is not None:
                session.expunge(row)
            return row

    @staticmethod
    def _validate_request(request_key: str, trigger: str, actor: str) -> None:
        try:
            parsed = uuid.UUID(request_key)
        except ValueError as error:
            raise CatalogSyncError(
                CatalogSyncCode.REQUEST_INVALID, "sync request key must be a UUID"
            ) from error
        if str(parsed) != request_key.lower():
            raise CatalogSyncError(
                CatalogSyncCode.REQUEST_INVALID, "sync request key must be canonical"
            )
        if trigger not in {"manual", "automatic"}:
            raise CatalogSyncError(
                CatalogSyncCode.TRIGGER_INVALID, "sync trigger is invalid"
            )
        if not actor.strip() or len(actor) > 200:
            raise CatalogSyncError(
                CatalogSyncCode.ACTOR_INVALID, "sync actor is invalid"
            )


CATALOG_SYNC_FIRST_RETRY_SECONDS = 30


def catalog_sync_retry_delay(failures: int, interval_seconds: int) -> int:
    """Seconds until the next automatic sync after ``failures`` in a row.

    A success waits the steady-state interval. Consecutive failures retry
    after 30, 60, 120, ... seconds, never later than that interval.
    """
    if failures <= 0:
        return interval_seconds
    return min(
        interval_seconds,
        CATALOG_SYNC_FIRST_RETRY_SECONDS * 2 ** min(failures - 1, 16),
    )


def catalog_sync_failure_reason(error: Exception) -> str:
    """Describe an automatic sync failure for the log: type, code, bounded detail.

    Only the typed ``detail`` the sync and reader raise is shown, never an
    arbitrary exception message, and it is bounded and stripped of control
    characters so an untrusted release cannot shape the log line.
    """
    code = getattr(error, "code", "unclassified")
    detail = getattr(error, "detail", None)
    text = (
        "".join(ch if ch.isprintable() else " " for ch in detail[:256])
        if isinstance(detail, str)
        else ""
    )
    return f"{type(error).__name__} ({str(code)[:128]})" + (f": {text}" if text else "")


async def run_automatic_sync(
    service: ManagedRecipeCatalogSyncService,
    stop: asyncio.Event,
    *,
    interval_seconds: int,
    settle_seconds: float = 10.0,
    reconcile: Callable[[], object] | None = None,
) -> None:
    """Run the automatic library sync until ``stop`` is set; it never dies.

    A sync that cannot finish (the library unreachable, another sync running, a
    document unreadable) is unknown, not failed for good: the previously imported
    catalog stays active and the sync is asked again on a delay that doubles from
    30 seconds up to ``interval_seconds``.
    """

    # Let migrations, health checks, and the local relay settle before the first
    # network-bound refresh. The durable ledger remains authoritative.
    try:
        await asyncio.wait_for(stop.wait(), timeout=settle_seconds)
        return
    except TimeoutError:
        pass
    failures = 0
    while not stop.is_set():
        try:
            # Each periodic tick starts a separate bounded observation epoch;
            # failure in catalog discovery cannot suppress standing key intent.
            if reconcile is not None:
                await asyncio.to_thread(reconcile)
            await asyncio.to_thread(lambda: service.automatic())
            failures = 0
        except (CatalogSyncError, RecipeLibraryError, OSError) as error:
            failures += 1
            _log_automatic_failure(error, failures, interval_seconds)
        except Exception as error:  # noqa: BLE001 - the loop must never die
            failures += 1
            _log_automatic_failure(error, failures, interval_seconds)
        try:
            await asyncio.wait_for(
                stop.wait(),
                timeout=catalog_sync_retry_delay(failures, interval_seconds),
            )
        except TimeoutError:
            continue


def _log_automatic_failure(
    error: Exception, failures: int, interval_seconds: int
) -> None:
    _LOGGER.warning(
        "automatic managed recipe catalog sync failed: %s; retrying in %s seconds",
        catalog_sync_failure_reason(error),
        catalog_sync_retry_delay(failures, interval_seconds),
    )


# An unreadable record is neither current nor failed: the next sync redoes it.
def _result(value: object) -> ManagedCatalogSyncResult:
    """Read a stored sync result; one that cannot be read is unknown, never fatal.

    The result is bookkeeping.  A damaged or foreign-version record reads as a
    partial result with one problem, so the sync that sees it is retried by the
    next automatic sync and overwrites the record; it never fails a sync or an
    install.
    """
    try:
        return ManagedCatalogSyncResult.model_validate_json(canonical_message(value))
    except (TypeError, ValueError):
        unknown = _empty_result()
        unknown.state = CatalogSyncState.PARTIAL
        unknown.problems = [
            ManagedCatalogSyncProblem(
                recipe_uri=None,
                code=CatalogSyncCode.RESULT_UNREADABLE,
                detail="stored catalog sync result was unreadable; it is re-synced",
            )
        ]
        return unknown


def _empty_result(reviewed_content: str | None = None) -> ManagedCatalogSyncResult:
    return ManagedCatalogSyncResult(
        reviewed_content_sha256=reviewed_content,
        schema_version=1,
        state=CatalogSyncState.CURRENT,
        imported_count=0,
        updated_count=0,
        unchanged_count=0,
        skipped_count=0,
        withdrawn_count=0,
        withdrawn_recipes=[],
        stale_recipes=[],
        problems=[],
    )


def _view(row: RecipeLibrarySyncRun | None) -> CatalogSyncView:
    if row is None:
        raise KeyError("sync run")
    result = _result(row.result)
    return CatalogSyncView(
        id=row.id,
        request_key=row.request_key,
        trigger=_TRIGGER.validate_python(row.trigger),
        state=CatalogSyncState.SYNCING
        if row.state == LifecycleState.RUNNING
        else result.state,
        repository=row.repository,
        expected_commit=row.expected_commit,
        commit=row.observed_commit,
        library_version=row.library_version,
        library_updated_at=(
            row.library_updated_at.replace(tzinfo=UTC)
            if row.library_updated_at is not None
            and row.library_updated_at.tzinfo is None
            else row.library_updated_at
        ),
        total_count=row.total_count,
        processed_count=row.processed_count,
        imported_count=row.imported_count,
        updated_count=row.updated_count,
        unchanged_count=row.current_count,
        skipped_count=row.conflict_count,
        withdrawn_count=result.withdrawn_count,
        withdrawn_recipes=tuple(result.withdrawn_recipes),
        stale_recipes=tuple(result.stale_recipes),
        problems=tuple(result.problems),
        created_at=row.created_at,
        completed_at=row.completed_at,
    )
