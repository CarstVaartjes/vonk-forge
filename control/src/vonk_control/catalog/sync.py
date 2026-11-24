"""Durable synchronization of the reviewed canonical recipe library."""

from __future__ import annotations

import io
import json
import logging
import random
import re
import time
import uuid
from collections.abc import Callable
from concurrent.futures import Executor, Future, ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from importlib import metadata
from typing import Protocol

from fastapi import HTTPException
from pydantic import TypeAdapter
from sqlalchemy import and_, func, select
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker
from vonk_forge_contracts import CatalogVocabulary, RecipeDisplay

from vonk_agent_protocol.agent_words import RunSwitchReasonScopeName
from vonk_agent_protocol.lifecycle_vocabulary import WaitReason
from vonk_agent_protocol.outcome import UnknownOutcomeError
from vonk_agent_protocol.reason_codes.catalog import CatalogSyncCode
from vonk_agent_protocol.state_machines import CatalogSyncState
from vonk_agent_protocol.wire_model import canonical_message
from vonk_control.admission_locking import ConstraintReplan, ConstraintReplay, admitting
from vonk_control.capabilities import CapabilityUnavailableError
from vonk_control.catalog.queries import active_head_revision
from vonk_control.catalog.revision_contract import catalog_document_readable
from vonk_control.catalog.service import CatalogService
from vonk_control.catalog.sync_contract import (
    SEMVER_PATTERN,
    CatalogSyncTrigger,
    ManagedCatalogStaleRecipe,
    ManagedCatalogSyncProblem,
    ManagedCatalogSyncRequest,
    ManagedCatalogSyncResult,
    ManagedCatalogWithdrawnRecipe,
    reviewed_catalog_content,
)
from vonk_control.models.cache import RecipeLibrarySyncRun
from vonk_control.models.catalog import CatalogDocumentRevision
from vonk_control.owner_liveness import OwnerKind, OwnerRef, owner
from vonk_control.recipe_packages.contracts import has_newer_major_note
from vonk_control.recipe_packages.library_types import (
    RecipeLibraryError,
    RecipeLibraryItem,
    RecipeLibrarySnapshot,
)
from vonk_control.source_bundles import SourceBundleUnknown
from vonk_control.stored.states import LibrarySyncRunState
from vonk_control.waits.contract import QueueWait, typed_wait_reason
from vonk_control.waits.persisted import read_wait, schedule_wait

_LOGGER = logging.getLogger(__name__)
_MAX_RESULT_ITEMS = 256


def _controller_marker() -> str:
    """Identify what this Controller can read, so an upgrade re-syncs the catalog."""
    parts = []
    for package in ("vonk-control", "vonk-forge-public-contracts"):
        try:
            parts.append(f"{package}={metadata.version(package)}")
        except metadata.PackageNotFoundError:
            parts.append(f"{package}=unknown")
    return ";".join(parts)[:128]


@dataclass(frozen=True)
class _AutomaticSyncStep:
    failures: int
    delay_seconds: int


class RecipeLibrarySyncReader(Protocol):
    def list(self) -> RecipeLibrarySnapshot: ...
    def prepare(self, snapshot: RecipeLibrarySnapshot) -> None: ...
    def fetch(self, uri: str) -> RecipeLibraryItem: ...


class CatalogSyncError(RuntimeError):
    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail[:256]
        super().__init__(self.detail)


class CatalogSyncUnsettled(UnknownOutcomeError, CatalogSyncError):
    """The sync could not settle: another sync owns the catalog, or the library
    moved on since it was read.  Unknown, asked again.

    ``AutomaticCatalogSync`` retries on its bounded backoff; nothing was applied.
    """

    def __init__(self, code: str, detail: str) -> None:
        CatalogSyncError.__init__(self, code, detail)
        self.typed_reason = WaitReason.OBSERVATION_UNAVAILABLE


SyncTrigger = CatalogSyncTrigger
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
    wait: QueueWait | None = None


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
        reader: RecipeLibrarySyncReader | None = None,
        clock: Callable[[], datetime],
        repository: str = "CarstVaartjes/vonk-forge-recipes",
    ) -> None:
        self._sessions = sessions
        self._catalog = catalog
        self._reader: RecipeLibrarySyncReader | None = reader
        self._clock = clock
        self._repository = repository

    def _required_reader(self) -> RecipeLibrarySyncReader:
        # The API only reads sync status; the worker owns the GitHub reader.
        if self._reader is None:
            raise CatalogSyncError(
                CatalogSyncCode.FAILED, "this process does not read the recipe library"
            )
        return self._reader

    def sync(self, request: ManagedCatalogSyncRequest) -> CatalogSyncView:
        request_key, trigger, actor = (
            request.request_key,
            request.trigger,
            request.actor,
        )
        reviewed_content = request.reviewed_content_sha256
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
            state=LibrarySyncRunState.RUNNING.value,
            active_slot="managed-recipes",
            repository=self._repository,
            expected_commit=None,
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
            with admitting(), self._sessions.begin() as session:
                active = session.scalar(
                    select(RecipeLibrarySyncRun).where(
                        RecipeLibrarySyncRun.state == LibrarySyncRunState.RUNNING
                    )
                )
                if active is not None and self._expired(session, active):
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
        except ConstraintReplay as duplicate:
            replay = duplicate.replay(lambda: self._by_request_key(request_key))
            if (replay.trigger, replay.actor) != (trigger, actor) or (
                replay.reviewed_content_sha256 != reviewed_content
            ):
                raise CatalogSyncError(
                    CatalogSyncCode.REQUEST_REUSED,
                    "request key was already used for different sync semantics",
                ) from duplicate
            return _view(replay)
        except ConstraintReplan as taken:
            # Another sync took the one active slot after this one looked.
            raise CatalogSyncUnsettled(
                CatalogSyncCode.IN_PROGRESS, "another managed catalog sync is running"
            ) from taken
        try:
            reader = self._required_reader()
            snapshot = reader.list()
            if (
                reviewed_content is not None
                and reviewed_catalog_content(snapshot) != reviewed_content
            ):
                raise CatalogSyncUnsettled(
                    CatalogSyncCode.PREVIEW_CHANGED,
                    "recipe library changed since it was reviewed",
                )
            reader.prepare(snapshot)
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

    def _expired(self, session: Session, run: RecipeLibrarySyncRun) -> bool:
        """Whether the running sync holding the slot has stopped heartbeating."""
        return not owner(
            session, OwnerRef(OwnerKind.SYNC_RUN, run.id), self._clock()
        ).live

    def _expire(self, run: RecipeLibrarySyncRun) -> None:
        failed = json.loads(canonical_message(_result(run.result)))
        failed["state"] = CatalogSyncState.FAILED.value
        run.state = LibrarySyncRunState.FAILED.value
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
                        RecipeLibrarySyncRun.state == LibrarySyncRunState.FAILED,
                        RecipeLibrarySyncRun.observed_commit.is_(None),
                    )
                )
            ) or session.scalar(newest)
            if row is None:
                return None
            failure = session.scalar(
                select(RecipeLibrarySyncRun)
                .where(RecipeLibrarySyncRun.state == LibrarySyncRunState.FAILED)
                .order_by(
                    RecipeLibrarySyncRun.completed_at.desc(),
                    RecipeLibrarySyncRun.id.desc(),
                )
                .limit(1)
            )
            succeeded = session.scalar(
                select(RecipeLibrarySyncRun.completed_at)
                .where(RecipeLibrarySyncRun.state == LibrarySyncRunState.SUCCEEDED)
                .order_by(RecipeLibrarySyncRun.completed_at.desc())
                .limit(1)
            )
            view = _view(row)
            waiting = session.scalar(
                newest.where(
                    RecipeLibrarySyncRun.repository == self._repository,
                    RecipeLibrarySyncRun.wait_evidence.is_not(None),
                )
            )
            if waiting is not None:
                view = replace(view, wait=read_wait(waiting))
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

    def advance_automatic(
        self, interval_seconds: int, failures: int = 0
    ) -> _AutomaticSyncStep:
        """One automatic observation and its durable next check."""
        errors: list[Exception] = []
        observed_id = None
        catalog_failed = True
        try:
            observation = self.automatic()
            observed_id = observation.id
            catalog_failed = observation.state != CatalogSyncState.CURRENT
            if catalog_failed:
                errors.append(
                    CatalogSyncUnsettled(
                        observation.problems[0].code
                        if observation.problems
                        else CatalogSyncCode.FAILED,
                        observation.problems[0].detail
                        if observation.problems
                        else "managed catalog observation unavailable",
                    )
                )
        except Exception as error:  # noqa: BLE001 - retain automatic observation intent
            errors.append(error)
        failures = failures + 1 if errors else 0
        delay = catalog_sync_retry_delay(failures, interval_seconds)
        try:
            self.record_automatic_wait(
                delay, failed=catalog_failed, observed_id=observed_id
            )
        except SQLAlchemyError as error:
            errors.append(error)
        for error in errors:
            _log_automatic_failure(error, delay)
        return _AutomaticSyncStep(failures, delay)

    def last_completed_at(self) -> datetime | None:
        """When the newest sync run finished: the API compares it to notice a sync."""
        with self._sessions() as session:
            return session.scalar(select(func.max(RecipeLibrarySyncRun.completed_at)))

    def held_elsewhere(self) -> bool:
        """Whether a live sync run holds the slot; its owner finishes it."""
        with self._sessions() as session:
            running = session.scalar(
                select(RecipeLibrarySyncRun).where(
                    RecipeLibrarySyncRun.state == LibrarySyncRunState.RUNNING
                )
            )
            return running is not None and not self._expired(session, running)

    def automatic_wait_delay(self) -> float:
        with self._sessions() as session:
            run = session.scalar(
                select(RecipeLibrarySyncRun)
                .where(
                    RecipeLibrarySyncRun.repository == self._repository,
                    RecipeLibrarySyncRun.wait_evidence.is_not(None),
                )
                .order_by(
                    RecipeLibrarySyncRun.created_at.desc(), RecipeLibrarySyncRun.id
                )
                .limit(1)
            )
            wait = read_wait(run) if run is not None else None
            return (
                max(0.0, (wait.next_check - self._clock()).total_seconds())
                if wait is not None
                else 0.0
            )

    def record_automatic_wait(
        self, delay_seconds: int, *, failed: bool, observed_id: str | None = None
    ) -> None:
        with self._sessions.begin() as session:
            run = (
                session.get(RecipeLibrarySyncRun, observed_id)
                if observed_id is not None
                else session.scalar(
                    select(RecipeLibrarySyncRun)
                    .where(RecipeLibrarySyncRun.repository == self._repository)
                    .order_by(
                        RecipeLibrarySyncRun.created_at.desc(), RecipeLibrarySyncRun.id
                    )
                    .limit(1)
                )
            )
            if run is not None:
                now = self._clock()
                previous = tuple(
                    session.scalars(
                        select(RecipeLibrarySyncRun).where(
                            RecipeLibrarySyncRun.repository == self._repository,
                            RecipeLibrarySyncRun.id != run.id,
                            RecipeLibrarySyncRun.wait_evidence.is_not(None),
                        )
                    )
                )
                for superseded in previous:
                    schedule_wait(superseded, None)
                schedule_wait(
                    run,
                    now + timedelta(seconds=delay_seconds),
                    now=now,
                    reason=typed_wait_reason(
                        run.error_code, WaitReason.OBSERVATION_UNAVAILABLE
                    )
                    if failed
                    else CatalogSyncCode.REFRESH_SCHEDULED,
                    detail=run.error_detail if failed else None,
                )

    def automatic(self) -> CatalogSyncView:
        try:
            snapshot = self._required_reader().list()
        except (RecipeLibraryError, CatalogSyncError, OSError) as error:
            # Reading the library failed before any sync could start; record
            # it so sync-status shows why the catalog is not advancing.
            self._record_read_failure(
                str(getattr(error, "code", CatalogSyncCode.FAILED)),
                str(getattr(error, "detail", str(error))) or type(error).__name__,
            )
            raise
        policy_problems = self._catalog.refresh_build_policy()
        with self._sessions() as session:
            current = session.scalar(
                select(RecipeLibrarySyncRun)
                .where(
                    RecipeLibrarySyncRun.state == LibrarySyncRunState.SUCCEEDED,
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
            current_view = (
                _view(current)
                if (
                    current is not None
                    and not policy_problems
                    and _result(current.result).state == CatalogSyncState.CURRENT
                    and current.controller_marker == _controller_marker()
                    # The newer-major note appears or clears with the listing:
                    # a stored result that disagrees is replaced by one sync.
                    and has_newer_major_note(_result(current.result).problems)
                    == has_newer_major_note(snapshot.display_notes)
                    # A Controller upgraded onto the same release has not stored
                    # the vocabulary yet: a normal sync writes it.
                    and (
                        snapshot.vocabulary is None
                        or isinstance(current.vocabulary, CatalogVocabulary)
                    )
                )
                else None
            )
        # Availability opens its own sessions and observes managed storage. Do
        # not retain the receipt transaction/connection while joining that work.
        if current_view is not None and self._snapshot_available(snapshot):
            return current_view
        return self.sync(
            ManagedCatalogSyncRequest(
                request_key=str(uuid.uuid4()),
                trigger=CatalogSyncTrigger.AUTOMATIC,
                actor="system:recipe-library-sync",
                reviewed_content_sha256=reviewed_catalog_content(snapshot),
            )
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
        digests = {model.entry.content_sha256 for model in snapshot.catalog_entities}
        if digests:
            # The catalog reader verified these digests at ingress.
            with self._sessions() as session:
                rows = {
                    row.content_digest: row
                    for row in session.scalars(
                        select(CatalogDocumentRevision).where(
                            CatalogDocumentRevision.content_digest.in_(sorted(digests))
                        )
                    )
                }
                if rows.keys() != digests:
                    return False
                heads = {
                    row.document_id: row
                    for row in session.scalars(
                        select(CatalogDocumentRevision).where(
                            CatalogDocumentRevision.document_id.in_(
                                sorted({row.document_id for row in rows.values()})
                            ),
                            active_head_revision(),
                        )
                    )
                }
                for row in rows.values():
                    selected = heads.get(row.document_id)
                    if selected is None:
                        return False
                    if not (
                        catalog_document_readable(row)
                        and catalog_document_readable(selected)
                    ):
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
                and latest.state == LibrarySyncRunState.FAILED
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
                    state=LibrarySyncRunState.FAILED.value,
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
        policy_problems = self._catalog.refresh_build_policy()
        # A sync that applies a library is where a Model projection written
        # under an earlier contract is re-derived; an unchanged tick never gets here.
        self._catalog.refresh_model_projections()
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
        for model in snapshot.catalog_entities:
            try:
                self._catalog.import_catalog_models(actor, [model.published])
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
                # A stale lookup is a miss. The verified incoming identity owns
                # this import; derived local bookkeeping cannot veto it.
                previous = None
            if (
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
                    hydrated = self._required_reader().fetch(item.uri)
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
        # Library text is one pass over the whole snapshot: a single read, then a
        # write for each document whose text changed. A failed write is that
        # document's problem; the next sync applies it again.
        recipe_uris = {(item.publisher, item.slug): item.uri for item in snapshot.items}
        for display, error in self._catalog.apply_displays(
            [
                model.entry.display
                for model in snapshot.catalog_entities
                if model.entry.display is not None
            ]
            + [item.display for item in snapshot.items if item.display is not None]
        ):
            self._record_problem_values(
                result,
                uri=recipe_uris.get((display.identity.publisher, display.identity.slug))
                if isinstance(display, RecipeDisplay)
                else None,
                code=str(getattr(error, "code", CatalogSyncCode.ITEM_FAILED)),
                detail=str(getattr(error, "detail", str(error))),
            )
        # Newest only: a recipe the published library no longer lists stops
        # being offered.  Installed and running revisions are untouched.  A
        # snapshot with skipped documents or no recipes may be incomplete, so
        # it retracts nothing; the next complete sync does.  Only the
        # Controller's own automatic sync of the published library retracts: a
        # manual or fixture sync (an operator import, the Spark canary) carries
        # a deliberately partial view and must never withdraw the rest.
        withdrawals_observed = False
        if trigger == "automatic" and snapshot.items and not snapshot.problems:
            withdrawals_observed = True
            retracted = self._catalog.retract_absent_from(
                [
                    (RunSwitchReasonScopeName.RECIPE, item.publisher, item.slug)
                    for item in snapshot.items
                ]
                + [
                    (
                        RunSwitchReasonScopeName.MODEL,
                        model.entry.document.identity.publisher,
                        model.entry.document.identity.slug,
                    )
                    for model in snapshot.catalog_entities
                ]
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
        # bundle first and adds image digests once the builds finish), and a
        # qualification status changes whenever new hardware evidence is
        # published, so every sync records both for unchanged revisions too.
        record_facts = getattr(self._catalog, "record_published_facts", None)
        if callable(record_facts):
            try:
                record_facts(
                    {
                        (item.publisher, item.slug, item.content_sha256): (
                            item.prebuilt_image
                        )
                        for item in snapshot.items
                        if item.prebuilt_image is not None or withdrawals_observed
                    },
                    {
                        (item.publisher, item.slug, item.content_sha256): (
                            item.qualification
                        )
                        for item in snapshot.items
                        if item.qualification is not None or withdrawals_observed
                    },
                    {
                        (item.publisher, item.slug, item.content_sha256): item.api
                        for item in snapshot.items
                        if item.api is not None or withdrawals_observed
                    },
                    {
                        (
                            item.publisher,
                            item.slug,
                            item.content_sha256,
                        ): item.measured_memory
                        for item in snapshot.items
                        if item.measured_memory is not None or withdrawals_observed
                    },
                )
            except Exception as error:  # noqa: BLE001 - images are optional; recipes still apply
                self._record_problem_values(
                    result,
                    uri=None,
                    code=CatalogSyncCode.PREBUILT_IMAGES_FAILED,
                    detail=str(error)[:256] or type(error).__name__,
                )
        if policy_problems:
            # Imports may have repaired or replaced the damaged head. Observe
            # once more before ending this episode; only unresolved policy
            # observations make it partial and schedule the bounded next sync.
            result.problems.extend(self._catalog.refresh_build_policy())
        result.state = (
            CatalogSyncState.PARTIAL if result.problems else CatalogSyncState.CURRENT
        )
        # An unreadable display is a note, decided after the state: it neither
        # makes the sync partial nor costs the unchanged-tick fast path.
        room = max(0, _MAX_RESULT_ITEMS - len(result.problems))
        result.problems.extend(snapshot.display_notes[:room])
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
            if run is None or run.state != LibrarySyncRunState.RUNNING:
                return False
            run.heartbeat_at = self._clock()
            run.observed_commit = snapshot.commit
            run.library_version = snapshot.version
            run.library_updated_at = snapshot.updated_at
            run.vocabulary = snapshot.vocabulary
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
            if run is None or run.state != LibrarySyncRunState.RUNNING:
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
            if run is None or run.state != LibrarySyncRunState.RUNNING:
                return
            run.state = LibrarySyncRunState.SUCCEEDED.value
            run.active_slot = None
            run.result = json.loads(canonical_message(result))
            run.missing_count = 0
            run.completed_at = self._clock()

    def _fail(self, run_id: str, code: str, detail: str) -> None:
        with self._sessions.begin() as session:
            run = session.get(RecipeLibrarySyncRun, run_id)
            if run is None or run.state != LibrarySyncRunState.RUNNING:
                return
            failed = _result(run.result)
            if len(failed.problems) < _MAX_RESULT_ITEMS:
                failed.problems.append(
                    ManagedCatalogSyncProblem(
                        recipe_uri=None, code=code[:128], detail=detail[:256]
                    )
                )
            failed.state = CatalogSyncState.FAILED
            run.state = LibrarySyncRunState.FAILED.value
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


CATALOG_SYNC_FIRST_RETRY_SECONDS = 30


def catalog_sync_retry_delay(failures: int, interval_seconds: int) -> int:
    """Seconds until the next automatic sync after ``failures`` in a row.

    A success waits the steady-state interval. Consecutive failures retry
    within the upper half of 30, 60, 120, ... seconds, capped at that interval.
    """
    if failures <= 0:
        return interval_seconds
    ceiling = min(
        interval_seconds,
        CATALOG_SYNC_FIRST_RETRY_SECONDS * 2 ** min(failures - 1, 16),
    )
    return random.randint(max(1, ceiling // 2), ceiling) if ceiling > 0 else 0


def catalog_sync_failure_reason(error: Exception) -> str:
    """Describe an automatic sync failure for the log: type, code, bounded detail.

    Only the typed ``detail`` the sync and reader raise is shown, never an
    arbitrary exception message, and it is bounded and stripped of control
    characters so an untrusted release cannot shape the log line.
    """
    code = getattr(error, "code", CatalogSyncCode.FAILED)
    detail = getattr(error, "detail", None)
    status = ""
    if isinstance(error, DBAPIError):
        sqlstate = getattr(error.orig, "sqlstate", None)
        if isinstance(sqlstate, str) and re.fullmatch(r"[0-9A-Z]{5}", sqlstate):
            # SQLAlchemy's code (e.g. 2j85) is a documentation category, not
            # the server condition. Never log raw SQL, parameters or messages.
            status = f"SQLSTATE {sqlstate}; "
    if isinstance(error, CapabilityUnavailableError):
        reply = error.reply
        detail = f"capability={reply.capability}; retryable={reply.retryable}"
    if isinstance(error, HTTPException):
        status = f"HTTP {error.status_code}; "
    text = (
        "".join(ch if ch.isprintable() else " " for ch in detail[:256])
        if isinstance(detail, str)
        else ""
    )
    return f"{type(error).__name__} ({status}{str(code)[:128]})" + (
        f": {text}" if text else ""
    )


class AutomaticCatalogSync:
    """The worker's automatic catalog cadence; no other process runs it.

    A new release is downloaded (hundreds of MB) and imported, so the sync runs
    on its own thread and never blocks the worker loop. The persisted wait is
    the schedule: a restarted worker keeps the delay the last attempt recorded,
    and an attempt that recorded nothing (the database was down) still waits
    its delay here. A sync that cannot finish (the library unreachable, another
    sync running, a document unreadable) keeps the previously imported catalog
    and is asked again with jittered backoff whose ceiling doubles from 30
    seconds up to ``interval_seconds``.
    """

    def __init__(
        self,
        service: ManagedRecipeCatalogSyncService,
        *,
        interval_seconds: int,
        monotonic: Callable[[], float] = time.monotonic,
        executor: Executor | None = None,
    ) -> None:
        self._service = service
        self._interval = interval_seconds
        self._monotonic = monotonic
        self._executor = executor or ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="library-reader"
        )
        self._active: Future[_AutomaticSyncStep] | None = None
        self._failures = 0
        self._not_before = monotonic()

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)

    def tick(self) -> bool:
        """Start the automatic sync when it is due; ``True`` when one started."""
        if self._active is not None:
            if not self._active.done():
                return False
            finished, self._active = self._active, None
            step = finished.result()
            self._failures = step.failures
            self._not_before = self._monotonic() + step.delay_seconds
        now = self._monotonic()
        if now < self._not_before:
            return False
        waiting = self._service.automatic_wait_delay()
        if waiting > 0:
            self._not_before = now + waiting
            return False
        if self._service.held_elsewhere():
            return False  # a request-led sync is importing; never read twice
        self._active = self._executor.submit(
            self._service.advance_automatic, self._interval, self._failures
        )
        return True


def _log_automatic_failure(error: Exception, delay: int) -> None:
    _LOGGER.warning(
        "automatic managed recipe catalog sync failed: %s; retrying in %s seconds",
        catalog_sync_failure_reason(error),
        delay,
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
    except TypeError, ValueError:
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
        wait=read_wait(row),
        id=row.id,
        request_key=row.request_key,
        trigger=_TRIGGER.validate_python(row.trigger),
        state=CatalogSyncState.SYNCING
        if row.state == LibrarySyncRunState.RUNNING
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
