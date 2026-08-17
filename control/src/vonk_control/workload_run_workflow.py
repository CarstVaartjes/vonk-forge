"""Preview and transactionally persist explainable WorkloadRun imports."""

from __future__ import annotations

import io
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .catalog_entities import CatalogError
from .import_report import ImportDisposition, ImportReportItem
from .import_resolution import resolve_import
from .model_resolution import ModelTransport
from .models import (
    LocalRecipe,
    LocalRecipeRevision,
    RecipeImport,
    RecipeImportItem,
    RecipeSourceBundle,
)
from .recipe_contract import recipe_content_sha256
from .registry_resolution import RegistryTransport
from .source_bundles import GeneratedSourceBundle, SourceBundleStore
from .workload_run_importer import WorkloadRunImportResult, import_workload_run
from .workload_run_source import parse_workload_run_yaml


class WorkloadRunWorkflowError(RuntimeError):
    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        super().__init__(detail)


@dataclass(frozen=True, slots=True)
class AppliedWorkloadRunImport:
    import_id: str
    recipe_id: str
    revision_id: str
    revision_number: int
    lifecycle: str
    source_sha256: str
    report_digest: str


@dataclass(frozen=True, slots=True)
class ResolvedWorkloadRunImport:
    recipe_id: str
    revision_id: str
    revision_number: int
    content_sha256: str


class WorkloadRunWorkflow:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        *,
        clock: Callable[[], datetime],
        bundles: SourceBundleStore,
        registry: RegistryTransport | None = None,
        models: ModelTransport | None = None,
        recipe_resolver: Callable[[Mapping[str, object], str], str] | None = None,
    ) -> None:
        self._sessions = sessions
        self._clock = clock
        self._bundles = bundles
        self._registry = registry
        self._models = models
        self._recipe_resolver = recipe_resolver

    def preview(self, raw: bytes) -> WorkloadRunImportResult:
        return import_workload_run(parse_workload_run_yaml(raw))

    def apply(
        self,
        raw: bytes,
        *,
        source_sha256: str,
        report_digest: str,
        actor: str,
    ) -> AppliedWorkloadRunImport:
        preview = self.preview(raw)
        if (
            preview.source_sha256 != source_sha256
            or preview.report_digest != report_digest
        ):
            raise WorkloadRunWorkflowError(
                "workload_run.stale_preview", "WorkloadRun preview identity changed"
            )
        stored_bundle = self._bundles.put(
            preview.bundle.sha256, io.BytesIO(preview.bundle.archive)
        )
        with self._sessions.begin() as session:
            existing = session.scalar(
                select(RecipeImport).where(
                    RecipeImport.source_kind == "workload_run",
                    RecipeImport.source_sha256 == source_sha256,
                )
            )
            if existing is not None:
                revision = session.scalar(
                    select(LocalRecipeRevision)
                    .where(LocalRecipeRevision.recipe_id == existing.recipe_id)
                    .order_by(LocalRecipeRevision.revision_number.desc())
                    .limit(1)
                )
                assert revision is not None
                return AppliedWorkloadRunImport(
                    existing.id,
                    existing.recipe_id,
                    revision.id,
                    revision.revision_number,
                    revision.lifecycle,
                    source_sha256,
                    report_digest,
                )
            document = preview.draft_document
            identity, metadata = document["identity"], document["metadata"]
            assert isinstance(identity, dict) and isinstance(metadata, dict)
            now = self._clock()
            recipe = LocalRecipe(
                slug=str(identity["slug"]),
                title=str(metadata["title"]),
                description=str(metadata["description"]),
                source_kind="workload_run",
                created_by=actor,
                created_at=now,
                updated_at=now,
            )
            session.add(recipe)
            session.flush()
            revision = LocalRecipeRevision(
                recipe_id=recipe.id,
                revision_number=1,
                lifecycle="blocked",
                schema_version=1,
                document=document,
                content_sha256=None,
                created_by=actor,
                created_at=now,
            )
            session.add(revision)
            _record_bundle(session, preview.bundle, stored_bundle.archive_bytes, now)
            imported = RecipeImport(
                recipe_id=recipe.id,
                source_kind="workload_run",
                source_reference=f"workload_run:sha256:{source_sha256}",
                source_sha256=source_sha256,
                redacted_source=preview.redacted_source,
                created_by=actor,
                created_at=now,
            )
            session.add(imported)
            session.flush()
            session.add_all(
                [
                    RecipeImportItem(
                        import_id=imported.id,
                        source_path=item.source_path,
                        disposition=item.disposition.value,
                        destination_path=item.destination_path,
                        reason_code=item.reason_code,
                        detail=item.detail,
                        blocking=item.blocking,
                    )
                    for item in preview.report
                ]
            )
            session.flush()
            return AppliedWorkloadRunImport(
                imported.id,
                recipe.id,
                revision.id,
                1,
                "blocked",
                source_sha256,
                report_digest,
            )

    def resolve(
        self,
        recipe_id: str,
        *,
        expected_revision: int,
        overlays: dict[str, object],
        actor: str,
    ) -> ResolvedWorkloadRunImport:
        if (
            self._registry is None
            or self._models is None
            or self._recipe_resolver is None
        ):
            raise WorkloadRunWorkflowError(
                "workload_run.resolution_unavailable",
                "external metadata resolution is unavailable",
            )
        with self._sessions() as session:
            recipe = session.get(LocalRecipe, recipe_id)
            imported = session.scalar(
                select(RecipeImport).where(RecipeImport.recipe_id == recipe_id)
            )
            revision = session.scalar(
                select(LocalRecipeRevision)
                .where(LocalRecipeRevision.recipe_id == recipe_id)
                .order_by(LocalRecipeRevision.revision_number.desc())
                .limit(1)
            )
            if recipe is None or imported is None or revision is None:
                raise KeyError(recipe_id)
            if (
                recipe.source_kind != "workload_run"
                or revision.revision_number != expected_revision
                or revision.lifecycle not in {"blocked", "draft"}
            ):
                raise WorkloadRunWorkflowError(
                    "catalog.stale_revision", "WorkloadRun draft revision changed"
                )
            rows = session.scalars(
                select(RecipeImportItem).where(
                    RecipeImportItem.import_id == imported.id
                )
            ).all()
            report = tuple(
                ImportReportItem(
                    row.source_path,
                    ImportDisposition(row.disposition),
                    row.destination_path,
                    row.reason_code,
                    row.detail,
                    row.blocking,
                )
                for row in rows
            )
            snapshot_id, snapshot_document = revision.id, revision.document
            build = snapshot_document.get("build")
            context = build.get("context") if isinstance(build, dict) else None
            digest = context.get("sha256") if isinstance(context, dict) else None
            if not isinstance(digest, str):
                raise WorkloadRunWorkflowError(
                    "workload_run.bundle_missing",
                    "import source bundle identity is missing",
                )
            bundle = self._bundles.get(digest)
            imported_result = WorkloadRunImportResult(
                draft_document=snapshot_document,
                bundle=bundle,
                report=report,
                source_sha256=imported.source_sha256,
                report_digest="",
                redacted_source=imported.redacted_source,
                runnable=False,
            )
        resolved = resolve_import(
            imported_result, overlays, registry=self._registry, models=self._models
        )
        if not resolved.runnable:
            codes = ", ".join(item.reason_code for item in resolved.blockers[:5])
            raise WorkloadRunWorkflowError(
                "workload_run.import_blocked",
                f"WorkloadRun import remains blocked: {codes}",
            )
        stored_bundle = self._bundles.put(
            resolved.bundle.sha256, io.BytesIO(resolved.bundle.archive)
        )
        try:
            digest = self._recipe_resolver(resolved.document, actor)
        except CatalogError as error:
            raise WorkloadRunWorkflowError(error.code, error.detail) from error
        if digest != recipe_content_sha256(resolved.document):
            raise WorkloadRunWorkflowError(
                "catalog.digest_mismatch", "catalog recipe digest is inconsistent"
            )
        now = self._clock()
        with self._sessions.begin() as session:
            current_recipe = session.scalar(
                select(LocalRecipe).where(LocalRecipe.id == recipe_id).with_for_update()
            )
            current = session.scalar(
                select(LocalRecipeRevision)
                .where(LocalRecipeRevision.recipe_id == recipe_id)
                .order_by(LocalRecipeRevision.revision_number.desc())
                .limit(1)
            )
            if current_recipe is None or current is None or current.id != snapshot_id:
                raise WorkloadRunWorkflowError(
                    "catalog.stale_revision", "WorkloadRun draft revision changed"
                )
            next_revision = LocalRecipeRevision(
                recipe_id=recipe_id,
                revision_number=expected_revision + 1,
                lifecycle="resolved",
                schema_version=1,
                document=resolved.document,
                content_sha256=digest,
                created_by=actor,
                created_at=now,
            )
            session.add(next_revision)
            _record_bundle(session, resolved.bundle, stored_bundle.archive_bytes, now)
            by_path = {item.source_path: item for item in resolved.report}
            for row in session.scalars(
                select(RecipeImportItem).where(
                    RecipeImportItem.import_id == imported.id
                )
            ):
                item = by_path[row.source_path]
                row.disposition = item.disposition.value
                row.reason_code = item.reason_code
                row.detail = item.detail
                row.blocking = item.blocking
                row.destination_path = item.destination_path
            session.flush()
            return ResolvedWorkloadRunImport(
                recipe_id, next_revision.id, next_revision.revision_number, digest
            )


def _record_bundle(
    session: Session,
    bundle: GeneratedSourceBundle,
    archive_bytes: int,
    now: datetime,
) -> None:
    if session.get(RecipeSourceBundle, bundle.sha256) is not None:
        return
    session.add(
        RecipeSourceBundle(
            sha256=bundle.sha256,
            media_type="application/vnd.vonk-forge.source-bundle.v1+tar",
            archive_bytes=archive_bytes,
            total_bytes=bundle.manifest.total_bytes,
            file_count=len(bundle.manifest.files),
            storage_key=f"{bundle.sha256[:2]}/{bundle.sha256}.tar",
            manifest={
                "schema_version": 1,
                "files": [asdict(item) for item in bundle.manifest.files],
                "total_bytes": bundle.manifest.total_bytes,
                "sha256": bundle.sha256,
            },
            verified_at=now,
        )
    )
