"""Durable synchronization of the reviewed canonical recipe library."""

from __future__ import annotations

import io
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from .catalog_service import CatalogError, CatalogService
from .models import RecipeLibrarySyncRun
from .recipe_library import RecipeLibraryError, RecipeLibraryItem, RecipeLibrarySnapshot

_MAX_RESULT_ITEMS = 256


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
            if (existing.trigger, existing.actor, existing.expected_commit) != (trigger, actor, expected_commit):
                raise CatalogSyncError("catalog.sync_request_reused", "request key was already used for different sync semantics")
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
            created_at=self._clock(),
            started_at=self._clock(),
            completed_at=None,
        )
        try:
            with self._sessions.begin() as session:
                active = session.scalar(select(RecipeLibrarySyncRun).where(RecipeLibrarySyncRun.state == "running"))
                if active is not None:
                    raise CatalogSyncError("catalog.sync_in_progress", f"managed catalog sync {active.id} is already running")
                session.add(run)
        except IntegrityError as error:
            replay = self._by_request_key(request_key)
            if replay is not None:
                return _view(replay)
            raise CatalogSyncError("catalog.sync_in_progress", "another managed catalog sync is running") from error
        try:
            snapshot = self._reader.list()
            if snapshot.repository != self._repository:
                raise CatalogSyncError("catalog.sync_repository_changed", "recipe library repository identity changed")
            if expected_commit is not None and snapshot.commit != expected_commit:
                raise CatalogSyncError("catalog.sync_preview_changed", "recipe library changed since it was reviewed")
            prepare = getattr(self._reader, "prepare", None)
            if callable(prepare):
                prepare(snapshot)
            self._initialize(run.id, snapshot)
            self._finish(run.id, self._apply(run.id, snapshot, actor=actor))
        except (CatalogError, RecipeLibraryError, CatalogSyncError) as error:
            code = str(getattr(error, "code", "catalog.sync_failed"))
            detail = str(getattr(error, "detail", str(error)))
            self._fail(run.id, code, detail)
            raise
        return self.get(run.id)

    def latest(self) -> CatalogSyncView | None:
        with self._sessions() as session:
            row = session.scalar(select(RecipeLibrarySyncRun).order_by(RecipeLibrarySyncRun.created_at.desc(), RecipeLibrarySyncRun.id.desc()).limit(1))
            return _view(row) if row is not None else None

    def get(self, sync_id: str) -> CatalogSyncView:
        with self._sessions() as session:
            row = session.get(RecipeLibrarySyncRun, sync_id)
            if row is None:
                raise KeyError(sync_id)
            return _view(row)

    def automatic(self) -> CatalogSyncView:
        snapshot = self._reader.list()
        with self._sessions() as session:
            current = session.scalar(
                select(RecipeLibrarySyncRun).where(
                    RecipeLibrarySyncRun.state == "succeeded",
                    RecipeLibrarySyncRun.observed_commit == snapshot.commit,
                ).order_by(RecipeLibrarySyncRun.completed_at.desc()).limit(1)
            )
            if current is not None:
                return _view(current)
        return self.sync(request_key=str(uuid.uuid4()), trigger="automatic", actor="system:recipe-library-sync", expected_commit=snapshot.commit)

    def _apply(self, run_id: str, snapshot: RecipeLibrarySnapshot, *, actor: str) -> dict[str, object]:
        result = _empty_result()
        self._catalog.import_catalog_models(actor, snapshot.catalog_entities)
        local = self._catalog.recipe_catalog_local_revisions([item.slug for item in snapshot.items])
        for item in snapshot.items:
            previous = local.get(item.slug)
            if previous is not None and (previous.publisher, previous.slug) != (item.publisher, item.slug):
                self._record_problem(result, item, "catalog.sync_identity_changed", "canonical recipe identity changed")
            elif previous is not None and previous.content_sha256 == item.content_sha256:
                result["unchanged_count"] = int(result["unchanged_count"]) + 1
            else:
                try:
                    hydrated = self._reader.fetch(item.uri)
                    if hydrated.library_commit != snapshot.commit or hydrated.content_sha256 != item.content_sha256:
                        raise CatalogSyncError("catalog.sync_revision_changed", "recipe changed while the exact library snapshot was applied")
                    if not _explicitly_executable(hydrated.document):
                        raise CatalogSyncError("catalog.sync_recipe_not_executable", "managed recipe does not explicitly declare an executable contract")
                    self._store_source_bundle(hydrated, actor)
                    self._catalog.import_recipe_library(
                        actor,
                        library_commit=hydrated.library_commit,
                        source_path=hydrated.source_path,
                        document=hydrated.document,
                        expected_content_sha256=hydrated.content_sha256,
                        dependency_documents=hydrated.dependencies,
                        release_version=hydrated.release_history[0].version if hydrated.release_history else None,
                        release_released_at=hydrated.release_history[0].released_at if hydrated.release_history else None,
                        package_handle=getattr(hydrated, "package_handle", None),
                        package_sha256=getattr(hydrated, "package_sha256", None),
                        source_bundle_sha256=getattr(hydrated, "source_bundle_sha256", None),
                    )
                    key = "imported_count" if previous is None else "updated_count"
                    result[key] = int(result[key]) + 1
                except (CatalogError, RecipeLibraryError, CatalogSyncError) as error:
                    self._record_problem(result, item, str(getattr(error, "code", "catalog.sync_item_failed")), str(getattr(error, "detail", str(error))))
            self._progress(run_id, result)
        result["state"] = "partial" if result["problems"] else "current"
        return result

    def _store_source_bundle(self, item: RecipeLibraryItem, actor: str) -> None:
        source_bundle = getattr(item, "source_bundle", None)
        source_digest = getattr(item, "source_bundle_sha256", None)
        if source_bundle is not None and isinstance(source_digest, str):
            self._catalog.store_source_bundle(source_digest, io.BytesIO(source_bundle), actor)

    def _record_problem(self, result: dict[str, object], item: RecipeLibraryItem, code: str, detail: str) -> None:
        result["skipped_count"] = int(result["skipped_count"]) + 1
        problems = list(result["problems"])
        if len(problems) < _MAX_RESULT_ITEMS:
            problems.append({"recipe_uri": item.uri, "code": code[:128], "detail": detail[:256]})
        result["problems"] = problems

    def _initialize(self, run_id: str, snapshot: RecipeLibrarySnapshot) -> None:
        with self._sessions.begin() as session:
            run = session.get(RecipeLibrarySyncRun, run_id)
            if run is None or run.state != "running":
                raise CatalogSyncError("catalog.sync_state_invalid", "managed catalog sync state changed")
            run.observed_commit = snapshot.commit
            run.total_count = len(snapshot.items)

    def _progress(self, run_id: str, result: Mapping[str, object]) -> None:
        with self._sessions.begin() as session:
            run = session.get(RecipeLibrarySyncRun, run_id)
            if run is None or run.state != "running":
                raise CatalogSyncError("catalog.sync_state_invalid", "managed catalog sync state changed")
            run.processed_count += 1
            run.imported_count = int(result["imported_count"])
            run.updated_count = int(result["updated_count"])
            run.current_count = int(result["unchanged_count"])
            run.conflict_count = int(result["skipped_count"])
            run.result = dict(result)

    def _finish(self, run_id: str, result: Mapping[str, object]) -> None:
        with self._sessions.begin() as session:
            run = session.get(RecipeLibrarySyncRun, run_id)
            if run is None or run.state != "running":
                raise CatalogSyncError("catalog.sync_state_invalid", "managed catalog sync state changed")
            run.state = "succeeded"
            run.active_slot = None
            run.result = dict(result)
            run.missing_count = 0
            run.completed_at = self._clock()

    def _fail(self, run_id: str, code: str, detail: str) -> None:
        with self._sessions.begin() as session:
            run = session.get(RecipeLibrarySyncRun, run_id)
            if run is None or run.state != "running":
                return
            failed = dict(run.result)
            problems = list(failed.get("problems", []))
            if len(problems) < _MAX_RESULT_ITEMS:
                problems.append({"recipe_uri": None, "code": code[:128], "detail": detail[:256]})
            failed["state"] = "failed"
            failed["problems"] = problems
            run.state = "failed"
            run.active_slot = None
            run.result = failed
            run.error_code = code[:128]
            run.error_detail = detail[:256]
            run.completed_at = self._clock()

    def _by_request_key(self, request_key: str) -> RecipeLibrarySyncRun | None:
        with self._sessions() as session:
            row = session.scalar(select(RecipeLibrarySyncRun).where(RecipeLibrarySyncRun.request_key == request_key))
            if row is not None:
                session.expunge(row)
            return row

    @staticmethod
    def _validate_request(request_key: str, trigger: str, actor: str, expected_commit: str | None) -> None:
        try:
            parsed = uuid.UUID(request_key)
        except ValueError as error:
            raise CatalogSyncError("catalog.sync_request_invalid", "sync request key must be a UUID") from error
        if str(parsed) != request_key.lower():
            raise CatalogSyncError("catalog.sync_request_invalid", "sync request key must be canonical")
        if trigger not in {"manual", "automatic"}:
            raise CatalogSyncError("catalog.sync_trigger_invalid", "sync trigger is invalid")
        if not actor.strip() or len(actor) > 200:
            raise CatalogSyncError("catalog.sync_actor_invalid", "sync actor is invalid")
        if expected_commit is not None and (len(expected_commit) != 40 or any(char not in "0123456789abcdef" for char in expected_commit)):
            raise CatalogSyncError("catalog.sync_commit_invalid", "expected commit must be lowercase Git SHA-1")


def _explicitly_executable(document: Mapping[str, object]) -> bool:
    metadata = document.get("metadata")
    tags = metadata.get("tags") if isinstance(metadata, Mapping) else None
    normalized = {str(value).lower() for value in tags} if isinstance(tags, list) else set()
    return "executable" in normalized and not normalized.intersection({"non-executable", "metadata-only", "integration-required"})


def _empty_result() -> dict[str, object]:
    return {"schema_version": 1, "state": "current", "imported_count": 0, "updated_count": 0, "unchanged_count": 0, "skipped_count": 0, "withdrawn_count": 0, "withdrawn_recipes": [], "stale_recipes": [], "problems": []}


def _view(row: RecipeLibrarySyncRun | None) -> CatalogSyncView:
    if row is None:
        raise KeyError("sync run")
    result = row.result or {}
    return CatalogSyncView(
        id=row.id,
        request_key=row.request_key,
        trigger=row.trigger,
        state="syncing" if row.state == "running" else str(result.get("state", row.state)),
        repository=row.repository,
        expected_commit=row.expected_commit,
        commit=row.observed_commit,
        total_count=row.total_count,
        processed_count=row.processed_count,
        imported_count=row.imported_count,
        updated_count=row.updated_count,
        unchanged_count=row.current_count,
        skipped_count=row.conflict_count,
        withdrawn_count=int(result.get("withdrawn_count", 0)),
        withdrawn_recipes=tuple(result.get("withdrawn_recipes", [])),
        stale_recipes=tuple(result.get("stale_recipes", [])),
        problems=tuple(result.get("problems", [])),
        created_at=row.created_at,
        completed_at=row.completed_at,
    )
