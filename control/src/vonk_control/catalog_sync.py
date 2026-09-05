"""Durable synchronization of the reviewed Vonk Forge recipe library."""

from __future__ import annotations

import io
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from .catalog_service import CatalogError, CatalogService
from .models import (
    LocalRecipe,
    LocalRecipeRevision,
    ManagedRecipeLibraryLink,
    RecipeImport,
    RecipeInstallation,
    RecipeLibrarySyncRun,
    RecipeRun,
)
from .recipe_library import (
    RecipeLibraryError,
    RecipeLibraryItem,
    RecipeLibrarySnapshot,
)

_ACTIVE_RUN_STATES = ("planned", "starting", "running", "stopping")
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
    """Synchronize only remote-managed recipes, never custom/local recipes."""

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
            if (
                existing.trigger != trigger
                or existing.actor != actor
                or existing.expected_commit != expected_commit
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
            created_at=self._clock(),
            started_at=self._clock(),
            completed_at=None,
        )
        try:
            with self._sessions.begin() as session:
                active = session.scalar(
                    select(RecipeLibrarySyncRun).where(
                        RecipeLibrarySyncRun.state == "running"
                    )
                )
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
            # Package readers validate every object in the candidate generation
            # before the first catalog link/revision is written.  The legacy
            # GitHub reader has no prepare hook and retains its established
            # per-item behavior.
            prepare = getattr(self._reader, "prepare", None)
            if callable(prepare):
                prepare(snapshot)
            self._initialize(run.id, snapshot)
            result = self._apply(run.id, snapshot, actor=actor)
            self._finish(run.id, result)
        except (CatalogError, RecipeLibraryError, CatalogSyncError) as error:
            code = getattr(error, "code", "catalog.sync_failed")
            detail = getattr(error, "detail", str(error))
            self._fail(run.id, str(code), str(detail))
            if isinstance(error, CatalogSyncError):
                raise
        return self.get(run.id)

    def latest(self) -> CatalogSyncView | None:
        with self._sessions() as session:
            row = session.scalar(
                select(RecipeLibrarySyncRun)
                .order_by(
                    RecipeLibrarySyncRun.created_at.desc(),
                    RecipeLibrarySyncRun.id.desc(),
                )
                .limit(1)
            )
            return _view(row) if row is not None else None

    def get(self, sync_id: str) -> CatalogSyncView:
        with self._sessions() as session:
            row = session.get(RecipeLibrarySyncRun, sync_id)
            if row is None:
                raise KeyError(sync_id)
            return _view(row)

    def automatic(self) -> CatalogSyncView:
        """Refresh once per observed immutable commit and retry failed attempts."""

        snapshot = self._reader.list()
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
            if current is not None:
                return _view(current)
        return self.sync(
            request_key=str(uuid.uuid4()),
            trigger="automatic",
            actor="system:recipe-library-sync",
            expected_commit=snapshot.commit,
        )

    def _apply(
        self, run_id: str, snapshot: RecipeLibrarySnapshot, *, actor: str
    ) -> dict[str, object]:
        result = _empty_result()
        package_mode = callable(getattr(self._reader, "prepare", None))
        local = self._catalog.recipe_catalog_local_revisions(
            [item.slug for item in snapshot.items]
        )
        seen: set[tuple[str, str]] = set()
        pending_links: list[tuple[RecipeLibraryItem, str]] = []
        for item in snapshot.items:
            seen.add((item.publisher, item.slug))
            recipe_uri = item.uri
            try:
                previous = local.get(item.slug)
                if previous is not None and (
                    previous.source_kind != "recipe_library"
                    or previous.publisher != item.publisher
                ):
                    raise CatalogSyncError(
                        "catalog.sync_custom_conflict",
                        "a custom or differently published recipe owns this slug",
                    )
                if previous is not None and previous.content_sha256 == item.content_sha256:
                    revision = self._revision_for_digest(
                        previous.recipe_id, item.content_sha256
                    )
                    if package_mode:
                        pending_links.append((item, revision.id))
                    else:
                        self._upsert_link(run_id, snapshot, item, revision.id)
                    result["unchanged_count"] = int(result["unchanged_count"]) + 1
                else:
                    hydrated = self._reader.fetch(item.uri)
                    if (
                        hydrated.library_commit != snapshot.commit
                        or hydrated.content_sha256 != item.content_sha256
                    ):
                        raise CatalogSyncError(
                            "catalog.sync_revision_changed",
                            "recipe changed while the exact library snapshot was applied",
                        )
                    if not _explicitly_executable(hydrated.document):
                        raise CatalogSyncError(
                            "catalog.sync_recipe_not_executable",
                            "managed recipe does not explicitly declare an executable contract",
                        )
                    self._store_source_bundle(hydrated, actor)
                    revision = self._catalog.import_recipe_library(
                        actor,
                        library_commit=hydrated.library_commit,
                        source_path=hydrated.source_path,
                        document=hydrated.document,
                        expected_content_sha256=hydrated.content_sha256,
                        dependency_documents=hydrated.dependencies,
                        release_version=(
                            hydrated.release_history[0].version
                            if hydrated.release_history
                            else None
                        ),
                        release_released_at=(
                            hydrated.release_history[0].released_at
                            if hydrated.release_history
                            else None
                        ),
                    )
                    if package_mode:
                        pending_links.append((item, revision.id))
                    else:
                        self._upsert_link(run_id, snapshot, item, revision.id)
                    key = "imported_count" if previous is None else "updated_count"
                    result[key] = int(result[key]) + 1
            except (CatalogError, RecipeLibraryError, CatalogSyncError) as error:
                if package_mode:
                    # A package candidate is a complete generation.  Keep the
                    # previous active links untouched if any item cannot be
                    # applied; only the durable run is marked failed by sync().
                    raise CatalogSyncError(
                        "catalog.sync_candidate_failed",
                        f"recipe package apply failed for {item.publisher}/{item.slug}",
                    ) from error
                self._record_item_error(run_id, snapshot, item, error)
                result["skipped_count"] = int(result["skipped_count"]) + 1
                problems = list(result["problems"])
                if len(problems) < _MAX_RESULT_ITEMS:
                    problems.append(
                        {
                            "recipe_uri": recipe_uri,
                            "code": str(getattr(error, "code", "catalog.sync_item_failed"))[:128],
                            "detail": str(getattr(error, "detail", str(error)))[:256],
                        }
                    )
                result["problems"] = problems
            self._progress(run_id, result)

        if package_mode:
            withdrawn, stale = self._commit_package_generation(
                run_id, snapshot, seen, pending_links
            )
        else:
            withdrawn, stale = self._reconcile_missing_and_stale(
                run_id, snapshot, seen
            )
        result["withdrawn_recipes"] = withdrawn
        result["withdrawn_count"] = len(withdrawn)
        result["stale_recipes"] = stale
        result["state"] = "partial" if result["problems"] else "current"
        return result

    def _store_source_bundle(self, item: RecipeLibraryItem, actor: str) -> None:
        if item.source_bundle is None:
            return
        build = item.document.get("build")
        context = build.get("context") if isinstance(build, Mapping) else None
        digest = context.get("sha256") if isinstance(context, Mapping) else None
        if not isinstance(digest, str):
            raise CatalogSyncError(
                "catalog.sync_source_invalid", "managed recipe source identity is invalid"
            )
        self._catalog.store_source_bundle(digest, io.BytesIO(item.source_bundle), actor)

    def _initialize(self, run_id: str, snapshot: RecipeLibrarySnapshot) -> None:
        with self._sessions.begin() as session:
            run = session.get(RecipeLibrarySyncRun, run_id)
            if run is None or run.state != "running":
                raise CatalogSyncError(
                    "catalog.sync_state_invalid", "managed catalog sync state changed"
                )
            run.observed_commit = snapshot.commit
            run.total_count = len(snapshot.items)

    def _progress(self, run_id: str, result: Mapping[str, object]) -> None:
        with self._sessions.begin() as session:
            run = session.get(RecipeLibrarySyncRun, run_id)
            if run is None or run.state != "running":
                raise CatalogSyncError(
                    "catalog.sync_state_invalid", "managed catalog sync state changed"
                )
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
                raise CatalogSyncError(
                    "catalog.sync_state_invalid", "managed catalog sync state changed"
                )
            run.state = "succeeded"
            run.active_slot = None
            run.result = dict(result)
            run.missing_count = int(result["withdrawn_count"])
            run.completed_at = self._clock()

    def _fail(self, run_id: str, code: str, detail: str) -> None:
        with self._sessions.begin() as session:
            run = session.get(RecipeLibrarySyncRun, run_id)
            if run is None or run.state != "running":
                return
            failed = dict(run.result)
            failed["state"] = "failed"
            problems = list(failed.get("problems", []))
            if len(problems) < _MAX_RESULT_ITEMS:
                problems.append({"recipe_uri": None, "code": code[:128], "detail": detail[:256]})
            failed["problems"] = problems
            run.state = "failed"
            run.active_slot = None
            run.result = failed
            run.error_code = code[:128]
            run.error_detail = detail[:256]
            run.completed_at = self._clock()

    def _revision_for_digest(
        self, recipe_id: str, content_sha256: str
    ) -> LocalRecipeRevision:
        with self._sessions() as session:
            revision = session.scalar(
                select(LocalRecipeRevision).where(
                    LocalRecipeRevision.recipe_id == recipe_id,
                    LocalRecipeRevision.content_sha256 == content_sha256,
                    LocalRecipeRevision.lifecycle == "resolved",
                )
            )
            if revision is None:
                raise CatalogSyncError(
                    "catalog.sync_history_inconsistent",
                    "managed recipe revision history is inconsistent",
                )
            session.expunge(revision)
            return revision

    def _upsert_link(
        self,
        run_id: str,
        snapshot: RecipeLibrarySnapshot,
        item: RecipeLibraryItem,
        revision_id: str,
    ) -> None:
        now = self._clock()
        with self._sessions.begin() as session:
            self._upsert_link_in_session(
                session, run_id, snapshot, item, revision_id, now=now
            )

    def _upsert_link_in_session(
        self,
        session: Session,
        run_id: str,
        snapshot: RecipeLibrarySnapshot,
        item: RecipeLibraryItem,
        revision_id: str,
        *,
        now: datetime,
    ) -> None:
        revision = session.get(LocalRecipeRevision, revision_id)
        if revision is None:
            raise CatalogSyncError(
                "catalog.sync_history_inconsistent", "managed recipe revision is missing"
            )
        recipe = session.get(LocalRecipe, revision.recipe_id)
        if recipe is None or recipe.source_kind != "recipe_library":
            raise CatalogSyncError(
                "catalog.sync_custom_conflict", "managed identity resolved to a custom recipe"
            )
        link = session.get(ManagedRecipeLibraryLink, recipe.id)
        if link is None:
            session.add(
                ManagedRecipeLibraryLink(
                    recipe_id=recipe.id,
                    repository=snapshot.repository,
                    publisher=item.publisher,
                    slug=item.slug,
                    source_path=item.source_path,
                    remote_commit=snapshot.commit,
                    remote_content_sha256=item.content_sha256,
                    local_revision_id=revision.id,
                    availability="present",
                    sync_state="current",
                    last_error=None,
                    last_seen_run_id=run_id,
                    first_synced_at=now,
                    updated_at=now,
                )
            )
        else:
            if (link.publisher, link.slug) != (item.publisher, item.slug):
                raise CatalogSyncError(
                    "catalog.sync_identity_changed",
                    "managed recipe publisher or slug changed",
                )
            link.source_path = item.source_path
            link.remote_commit = snapshot.commit
            link.remote_content_sha256 = item.content_sha256
            link.local_revision_id = revision.id
            link.availability = "present"
            link.sync_state = "current"
            link.last_error = None
            link.last_seen_run_id = run_id
            link.updated_at = now

    def _record_item_error(
        self,
        run_id: str,
        snapshot: RecipeLibrarySnapshot,
        item: RecipeLibraryItem,
        error: Exception,
    ) -> None:
        """Expose a failed managed update without ever claiming custom authority."""

        with self._sessions.begin() as session:
            recipe = session.scalar(
                select(LocalRecipe).where(LocalRecipe.slug == item.slug)
            )
            if recipe is None or recipe.source_kind != "recipe_library":
                return
            link = session.get(ManagedRecipeLibraryLink, recipe.id)
            if link is None or (link.publisher, link.slug) != (
                item.publisher,
                item.slug,
            ):
                return
            link.source_path = item.source_path
            link.remote_commit = snapshot.commit
            link.remote_content_sha256 = item.content_sha256
            link.availability = "present"
            link.sync_state = "update-available"
            link.last_error = str(getattr(error, "detail", str(error)))[:256]
            link.last_seen_run_id = run_id
            link.updated_at = self._clock()

    def _reconcile_missing_and_stale(
        self,
        run_id: str,
        snapshot: RecipeLibrarySnapshot,
        seen: set[tuple[str, str]],
    ) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
        with self._sessions.begin() as session:
            withdrawn, stale = self._reconcile_in_session(
                session, run_id, snapshot, seen, now=self._clock()
            )
        return withdrawn, stale

    def _commit_package_generation(
        self,
        run_id: str,
        snapshot: RecipeLibrarySnapshot,
        seen: set[tuple[str, str]],
        pending_links: list[tuple[RecipeLibraryItem, str]],
    ) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
        """Publish one complete package candidate as the active generation."""

        with self._sessions.begin() as session:
            now = self._clock()
            for item, revision_id in pending_links:
                self._upsert_link_in_session(
                    session, run_id, snapshot, item, revision_id, now=now
                )
            return self._reconcile_in_session(
                session, run_id, snapshot, seen, now=now
            )

    def _reconcile_in_session(
        self,
        session: Session,
        run_id: str,
        snapshot: RecipeLibrarySnapshot,
        seen: set[tuple[str, str]],
        *,
        now: datetime,
    ) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
        withdrawn: list[dict[str, object]] = []
        stale: list[dict[str, object]] = []
        links = list(
            session.scalars(
                select(ManagedRecipeLibraryLink).where(
                    ManagedRecipeLibraryLink.repository == snapshot.repository
                )
            )
        )
        for link in links:
            if (link.publisher, link.slug) not in seen:
                link.availability = "missing"
                link.updated_at = now
            stale_installations, stale_runs, installed, running = _operational_counts(
                session, link.recipe_id, link.local_revision_id
            )
            if (
                link.availability == "missing"
                and (installed or running)
                and len(withdrawn) < _MAX_RESULT_ITEMS
            ):
                withdrawn.append(
                    {
                        "recipe_id": link.recipe_id,
                        "recipe_uri": (
                            f"vonk://catalog/{link.publisher}/{link.slug}"
                            f"@sha256:{link.remote_content_sha256}"
                        ),
                        "release_version": _release_version(
                            session, link.recipe_id, link.remote_content_sha256
                        ),
                        "model_version_key": _model_version_key(
                            session, link.local_revision_id
                        ),
                    }
                )
            if (stale_installations or stale_runs) and len(stale) < _MAX_RESULT_ITEMS:
                stale.append(
                    {
                        "recipe_id": link.recipe_id,
                        "current_revision_id": link.local_revision_id,
                        "stale_installation_count": stale_installations,
                        "stale_run_count": stale_runs,
                    }
                )
        run = session.get(RecipeLibrarySyncRun, run_id)
        if run is not None:
            run.missing_count = len(withdrawn)
        return withdrawn, stale

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
            raise CatalogSyncError("catalog.sync_actor_invalid", "sync actor is invalid")
        if expected_commit is not None and (
            len(expected_commit) != 40
            or any(character not in "0123456789abcdef" for character in expected_commit)
        ):
            raise CatalogSyncError(
                "catalog.sync_commit_invalid", "expected commit must be lowercase Git SHA-1"
            )


def _explicitly_executable(document: Mapping[str, object]) -> bool:
    metadata = document.get("metadata")
    tags = metadata.get("tags") if isinstance(metadata, Mapping) else None
    normalized = {str(value).lower() for value in tags} if isinstance(tags, list) else set()
    return "executable" in normalized and not normalized.intersection(
        {"non-executable", "metadata-only", "integration-required"}
    )


def _operational_counts(
    session: Session, recipe_id: str, current_revision_id: str
) -> tuple[int, int, int, int]:
    installations = int(
        session.scalar(
            select(func.count(RecipeInstallation.id))
            .join(
                LocalRecipeRevision,
                LocalRecipeRevision.id == RecipeInstallation.recipe_revision_id,
            )
            .where(
                LocalRecipeRevision.recipe_id == recipe_id,
                RecipeInstallation.state != "uninstalled",
            )
        )
        or 0
    )
    stale_installations = int(
        session.scalar(
            select(func.count(RecipeInstallation.id))
            .join(
                LocalRecipeRevision,
                LocalRecipeRevision.id == RecipeInstallation.recipe_revision_id,
            )
            .where(
                LocalRecipeRevision.recipe_id == recipe_id,
                RecipeInstallation.state != "uninstalled",
                RecipeInstallation.recipe_revision_id != current_revision_id,
            )
        )
        or 0
    )
    runs = int(
        session.scalar(
            select(func.count(RecipeRun.id))
            .join(
                RecipeInstallation,
                RecipeInstallation.id == RecipeRun.installation_id,
            )
            .join(
                LocalRecipeRevision,
                LocalRecipeRevision.id == RecipeInstallation.recipe_revision_id,
            )
            .where(
                LocalRecipeRevision.recipe_id == recipe_id,
                RecipeRun.state.in_(_ACTIVE_RUN_STATES),
            )
        )
        or 0
    )
    stale_runs = int(
        session.scalar(
            select(func.count(RecipeRun.id))
            .join(
                RecipeInstallation,
                RecipeInstallation.id == RecipeRun.installation_id,
            )
            .join(
                LocalRecipeRevision,
                LocalRecipeRevision.id == RecipeInstallation.recipe_revision_id,
            )
            .where(
                LocalRecipeRevision.recipe_id == recipe_id,
                RecipeRun.state.in_(_ACTIVE_RUN_STATES),
                RecipeInstallation.recipe_revision_id != current_revision_id,
            )
        )
        or 0
    )
    return stale_installations, stale_runs, installations, runs


def _model_version_key(session: Session, revision_id: str) -> str | None:
    revision = session.get(LocalRecipeRevision, revision_id)
    if revision is None:
        return None
    model = revision.document.get("model")
    if not isinstance(model, Mapping):
        return None
    publisher, slug, digest = (
        model.get("publisher"),
        model.get("slug"),
        model.get("content_sha256"),
    )
    if not all(isinstance(value, str) and value for value in (publisher, slug, digest)):
        return None
    return f"{publisher}/{slug}@{digest}"


def _release_version(
    session: Session, recipe_id: str, content_sha256: str
) -> str | None:
    receipt = session.scalar(
        select(RecipeImport)
        .where(
            RecipeImport.recipe_id == recipe_id,
            RecipeImport.source_kind == "recipe_library",
            RecipeImport.source_sha256 == content_sha256,
        )
        .order_by(RecipeImport.created_at.desc())
        .limit(1)
    )
    if receipt is None:
        return None
    value = receipt.redacted_source.get("release_version")
    return value if isinstance(value, str) else None


def _empty_result() -> dict[str, object]:
    return {
        "schema_version": 1,
        "state": "current",
        "imported_count": 0,
        "updated_count": 0,
        "unchanged_count": 0,
        "skipped_count": 0,
        "withdrawn_count": 0,
        "withdrawn_recipes": [],
        "stale_recipes": [],
        "problems": [],
    }


def _view(row: RecipeLibrarySyncRun) -> CatalogSyncView:
    result = row.result
    state = (
        "syncing"
        if row.state == "running"
        else str(
            result.get("state", "failed" if row.state == "failed" else "current")
        )
    )
    return CatalogSyncView(
        id=row.id,
        request_key=row.request_key,
        trigger=row.trigger,
        state=state,
        repository=row.repository,
        expected_commit=row.expected_commit,
        commit=row.observed_commit,
        total_count=row.total_count,
        processed_count=row.processed_count,
        imported_count=int(result.get("imported_count", row.imported_count)),
        updated_count=int(result.get("updated_count", row.updated_count)),
        unchanged_count=int(result.get("unchanged_count", row.current_count)),
        skipped_count=int(result.get("skipped_count", row.conflict_count)),
        withdrawn_count=int(result.get("withdrawn_count", row.missing_count)),
        withdrawn_recipes=tuple(result.get("withdrawn_recipes", [])),
        stale_recipes=tuple(result.get("stale_recipes", [])),
        problems=tuple(result.get("problems", [])),
        created_at=row.created_at,
        completed_at=row.completed_at,
    )


__all__ = [
    "CatalogSyncError",
    "CatalogSyncView",
    "ManagedRecipeCatalogSyncService",
]
