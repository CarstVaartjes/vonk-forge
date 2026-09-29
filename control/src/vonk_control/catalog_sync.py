"""Durable synchronization of the reviewed canonical recipe library."""

from __future__ import annotations

import io
import json
import logging
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from importlib import metadata
from typing import Protocol

from sqlalchemy import and_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import canonical_message

from .bounded_json import require_integer, require_sequence
from .catalog_service import CatalogService
from .catalog_sync_contract import ManagedCatalogSyncResult
from .models import RecipeLibrarySyncRun
from .recipe_library_types import (
    RecipeLibraryError,
    RecipeLibraryItem,
    RecipeLibrarySnapshot,
)

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


@dataclass(frozen=True, slots=True)
class CatalogSyncView:
    id: str
    request_key: str
    trigger: str
    state: str
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
    withdrawn_recipes: tuple[dict[str, object], ...]
    stale_recipes: tuple[dict[str, object], ...]
    problems: tuple[dict[str, object], ...]
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
        expected_commit: str | None = None,
    ) -> CatalogSyncView:
        self._validate_request(request_key, trigger, actor, expected_commit)
        existing = self._by_request_key(request_key)
        if existing is not None:
            if (existing.trigger, existing.actor, existing.expected_commit) != (
                trigger,
                actor,
                expected_commit,
            ):
                raise CatalogSyncError(
                    "catalog.sync_request_reused",
                    "request key was already used for different sync semantics",
                )
            return _view(existing)
        run = RecipeLibrarySyncRun(
            request_key=request_key,
            trigger=trigger,
            state="running",
            active_slot="managed-recipes",
            repository=self._repository,
            expected_commit=expected_commit,
            observed_commit=None,
            total_count=0,
            processed_count=0,
            imported_count=0,
            updated_count=0,
            current_count=0,
            conflict_count=0,
            missing_count=0,
            result=_empty_result(),
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
                    raise CatalogSyncError(
                        "catalog.sync_in_progress",
                        f"managed catalog sync {active.id} is already running",
                    )
                session.add(run)
        except IntegrityError as error:
            replay = self._by_request_key(request_key)
            if replay is not None:
                return _view(replay)
            raise CatalogSyncError(
                "catalog.sync_in_progress", "another managed catalog sync is running"
            ) from error
        try:
            snapshot = self._reader.list()
            if snapshot.repository != self._repository:
                raise CatalogSyncError(
                    "catalog.sync_repository_changed",
                    "recipe library repository identity changed",
                )
            if expected_commit is not None and snapshot.commit != expected_commit:
                raise CatalogSyncError(
                    "catalog.sync_preview_changed",
                    "recipe library changed since it was reviewed",
                )
            prepare = getattr(self._reader, "prepare", None)
            if callable(prepare):
                prepare(snapshot)
            self._initialize(run.id, snapshot)
            self._finish(run.id, self._apply(run.id, snapshot, actor=actor))
        except Exception as error:
            # Whatever went wrong, never leave the run "running": it would
            # block every later sync until its lease expired.
            code = str(getattr(error, "code", "catalog.sync_failed"))
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
        failed["state"] = "failed"
        run.state = "failed"
        run.active_slot = None
        run.result = json.loads(canonical_message(_result(failed)))
        run.error_code = "catalog.sync_lease_expired"
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
                    code=failure.error_code or "catalog.sync_failed",
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
                str(getattr(error, "code", "catalog.sync_failed")),
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
                and _result(current.result).state == "current"
                and current.controller_marker == _controller_marker()
            ):
                return _view(current)
        return self.sync(
            request_key=str(uuid.uuid4()),
            trigger="automatic",
            actor="system:recipe-library-sync",
            expected_commit=snapshot.commit,
        )

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
            failed = json.loads(canonical_message(_result(_empty_result())))
            failed["state"] = "failed"
            failed["problems"] = [{"recipe_uri": None, "code": code, "detail": detail}]
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
                    result=json.loads(canonical_message(_result(failed))),
                    error_code=code,
                    error_detail=detail,
                    actor="system:recipe-library-sync",
                    created_at=now,
                    started_at=now,
                    completed_at=now,
                )
            )

    def _apply(
        self, run_id: str, snapshot: RecipeLibrarySnapshot, *, actor: str
    ) -> dict[str, object]:
        result = _empty_result()
        self._catalog.refresh_build_policy()
        # Index documents the reader could not validate were already skipped;
        # report each one without holding up the rest of the snapshot.
        for problem in snapshot.problems:
            uri = problem.get("recipe_uri")
            self._record_problem_values(
                result,
                uri=uri if isinstance(uri, str) else None,
                code=str(problem.get("code", "catalog.sync_item_failed")),
                detail=str(problem.get("detail", "catalog document was skipped")),
            )
            self._progress(run_id, result)
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
                    code=str(getattr(error, "code", "catalog.sync_model_failed")),
                    detail=str(getattr(error, "detail", str(error))),
                )
            self._progress(run_id, result)
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
                    "catalog.sync_identity_changed",
                    "canonical recipe identity changed",
                )
            elif (
                previous is not None and previous.content_sha256 == item.content_sha256
            ):
                result["unchanged_count"] = (
                    require_integer(
                        result["unchanged_count"],
                        "stored catalog sync unchanged_count is invalid",
                    )
                    + 1
                )
            else:
                try:
                    hydrated = self._reader.fetch(item.uri)
                    if (
                        hydrated.library_commit != snapshot.commit
                        or hydrated.content_sha256 != item.content_sha256
                    ):
                        raise CatalogSyncError(
                            "catalog.sync_revision_changed",
                            "recipe changed while the exact library snapshot was applied",
                        )
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
                    key = "imported_count" if previous is None else "updated_count"
                    result[key] = (
                        require_integer(
                            result[key], "stored catalog sync count is invalid"
                        )
                        + 1
                    )
                except Exception as error:  # noqa: BLE001 - isolate untyped fetch failures per recipe
                    self._record_problem(
                        result,
                        item,
                        str(getattr(error, "code", "catalog.sync_item_failed")),
                        str(getattr(error, "detail", str(error))),
                    )
            self._progress(run_id, result)
        result["state"] = "partial" if result["problems"] else "current"
        return result

    def _store_source_bundle(self, item: RecipeLibraryItem, actor: str) -> None:
        source_bundle = getattr(item, "source_bundle", None)
        source_digest = getattr(item, "source_bundle_sha256", None)
        if source_bundle is not None and isinstance(source_digest, str):
            self._catalog.store_source_bundle(
                source_digest, io.BytesIO(source_bundle), actor
            )

    def _record_problem(
        self, result: dict[str, object], item: RecipeLibraryItem, code: str, detail: str
    ) -> None:
        self._record_problem_values(result, uri=item.uri, code=code, detail=detail)

    def _record_problem_values(
        self,
        result: dict[str, object],
        *,
        uri: str | None,
        code: str,
        detail: str,
    ) -> None:
        result["skipped_count"] = (
            require_integer(
                result["skipped_count"], "stored catalog sync skipped_count is invalid"
            )
            + 1
        )
        problems = list(
            require_sequence(
                result["problems"], "stored catalog sync problems are invalid"
            )
        )
        if len(problems) < _MAX_RESULT_ITEMS:
            problems.append(
                {"recipe_uri": uri, "code": code[:128], "detail": detail[:256]}
            )
        result["problems"] = problems

    def _initialize(self, run_id: str, snapshot: RecipeLibrarySnapshot) -> None:
        with self._sessions.begin() as session:
            run = session.get(RecipeLibrarySyncRun, run_id)
            if run is None or run.state != "running":
                raise CatalogSyncError(
                    "catalog.sync_state_invalid", "managed catalog sync state changed"
                )
            run.heartbeat_at = self._clock()
            run.observed_commit = snapshot.commit
            run.library_version = snapshot.version
            run.library_updated_at = snapshot.updated_at
            run.total_count = (
                len(snapshot.items)
                + len(snapshot.catalog_entities)
                + len(snapshot.problems)
            )

    def _progress(self, run_id: str, result: Mapping[str, object]) -> None:
        with self._sessions.begin() as session:
            run = session.get(RecipeLibrarySyncRun, run_id)
            if run is None or run.state != "running":
                raise CatalogSyncError(
                    "catalog.sync_state_invalid", "managed catalog sync state changed"
                )
            parsed = _result(result)
            run.heartbeat_at = self._clock()
            run.processed_count += 1
            run.imported_count = parsed.imported_count
            run.updated_count = parsed.updated_count
            run.current_count = parsed.unchanged_count
            run.conflict_count = parsed.skipped_count
            run.result = json.loads(canonical_message(parsed))

    def _finish(self, run_id: str, result: Mapping[str, object]) -> None:
        with self._sessions.begin() as session:
            run = session.get(RecipeLibrarySyncRun, run_id)
            if run is None or run.state != "running":
                raise CatalogSyncError(
                    "catalog.sync_state_invalid", "managed catalog sync state changed"
                )
            run.state = "succeeded"
            run.active_slot = None
            run.result = json.loads(canonical_message(_result(result)))
            run.missing_count = 0
            run.completed_at = self._clock()

    def _fail(self, run_id: str, code: str, detail: str) -> None:
        with self._sessions.begin() as session:
            run = session.get(RecipeLibrarySyncRun, run_id)
            if run is None or run.state != "running":
                return
            failed = json.loads(canonical_message(_result(run.result)))
            problems = list(failed["problems"])
            if len(problems) < _MAX_RESULT_ITEMS:
                problems.append(
                    {"recipe_uri": None, "code": code[:128], "detail": detail[:256]}
                )
            failed["state"] = "failed"
            failed["problems"] = problems
            run.state = "failed"
            run.active_slot = None
            run.result = json.loads(canonical_message(_result(failed)))
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
    def _validate_request(
        request_key: str, trigger: str, actor: str, expected_commit: str | None
    ) -> None:
        try:
            parsed = uuid.UUID(request_key)
        except ValueError as error:
            raise CatalogSyncError(
                "catalog.sync_request_invalid", "sync request key must be a UUID"
            ) from error
        if str(parsed) != request_key.lower():
            raise CatalogSyncError(
                "catalog.sync_request_invalid", "sync request key must be canonical"
            )
        if trigger not in {"manual", "automatic"}:
            raise CatalogSyncError(
                "catalog.sync_trigger_invalid", "sync trigger is invalid"
            )
        if not actor.strip() or len(actor) > 200:
            raise CatalogSyncError(
                "catalog.sync_actor_invalid", "sync actor is invalid"
            )
        if expected_commit is not None and (
            len(expected_commit) != 40
            or any(char not in "0123456789abcdef" for char in expected_commit)
        ):
            raise CatalogSyncError(
                "catalog.sync_commit_invalid",
                "expected commit must be lowercase Git SHA-1",
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


def _result(value: object) -> ManagedCatalogSyncResult:
    try:
        return ManagedCatalogSyncResult.model_validate_json(canonical_message(value))
    except (TypeError, ValueError) as error:
        raise CatalogSyncError(
            "catalog.sync_result_invalid", "stored catalog sync result is invalid"
        ) from error


def _empty_result() -> dict[str, object]:
    return json.loads(
        canonical_message(
            ManagedCatalogSyncResult(
                schema_version=1,
                state="current",
                imported_count=0,
                updated_count=0,
                unchanged_count=0,
                skipped_count=0,
                withdrawn_count=0,
                withdrawn_recipes=[],
                stale_recipes=[],
                problems=[],
            )
        )
    )


def _view(row: RecipeLibrarySyncRun | None) -> CatalogSyncView:
    if row is None:
        raise KeyError("sync run")
    result = _result(row.result)
    return CatalogSyncView(
        id=row.id,
        request_key=row.request_key,
        trigger=row.trigger,
        state="syncing" if row.state == "running" else result.state,
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
        withdrawn_recipes=tuple(
            json.loads(canonical_message(item)) for item in result.withdrawn_recipes
        ),
        stale_recipes=tuple(
            json.loads(canonical_message(item)) for item in result.stale_recipes
        ),
        problems=tuple(json.loads(canonical_message(item)) for item in result.problems),
        created_at=row.created_at,
        completed_at=row.completed_at,
    )
