"""Preview and transactionally persist explainable WorkloadRun imports."""

from __future__ import annotations

import io
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from vonk_forge_contracts import RecipeDefinition, content_sha256

from .catalog_entities import CatalogEntityService, CatalogError
from .import_resolution import resolve_import
from .model_resolution import ModelTransport
from .models import (
    CatalogDocument,
    CatalogDocumentRevision,
    RecipeSourceBundle,
)
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
                select(CatalogDocumentRevision).where(
                    CatalogDocumentRevision.kind == "recipe",
                    CatalogDocumentRevision.state == "candidate",
                    CatalogDocumentRevision.projected["source_kind"].as_string()
                    == "workload_run",
                    CatalogDocumentRevision.projected["source_sha256"].as_string()
                    == source_sha256,
                )
            )
            if existing is not None:
                return AppliedWorkloadRunImport(
                    existing.id,
                    existing.document_id,
                    existing.id,
                    existing.revision_number,
                    existing.state,
                    source_sha256,
                    report_digest,
            )
            document = preview.draft_document
            try:
                parsed = RecipeDefinition.model_validate(document)
            except (TypeError, ValueError) as error:
                raise WorkloadRunWorkflowError(
                    "catalog.document_invalid",
                    "WorkloadRun import must produce a canonical RecipeDefinition",
                ) from error
            now = self._clock()
            revision = CatalogEntityService(session, clock=self._clock).create_draft(
                parsed.model_dump(mode="json"), actor=actor
            )
            revision.projected = {
                **revision.projected,
                "source_kind": "workload_run",
                "source_sha256": source_sha256,
                "report": [
                    {**asdict(item), "disposition": item.disposition.value}
                    for item in preview.report
                ],
                "redacted_source": preview.redacted_source,
                "source_bundle_sha256": preview.bundle.sha256,
            }
            _record_bundle(session, preview.bundle, stored_bundle.archive_bytes, now)
            session.flush()
            return AppliedWorkloadRunImport(
                revision.id,
                revision.document_id,
                revision.id,
                1,
                revision.state,
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
            recipe = session.get(CatalogDocument, recipe_id)
            revision = session.scalar(
                select(CatalogDocumentRevision)
                .where(
                    CatalogDocumentRevision.document_id == recipe_id,
                    CatalogDocumentRevision.state == "candidate",
                )
                .order_by(CatalogDocumentRevision.revision_number.desc())
                .limit(1)
            )
            if recipe is None or revision is None:
                raise KeyError(recipe_id)
            if (
                revision.kind != "recipe"
                or revision.revision_number != expected_revision
                or revision.projected.get("source_kind") != "workload_run"
            ):
                raise WorkloadRunWorkflowError(
                    "catalog.stale_revision", "WorkloadRun draft revision changed"
                )
            report: tuple = ()
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
                source_sha256=str(revision.projected.get("source_sha256", "")),
                report_digest="",
                redacted_source=dict(revision.projected.get("redacted_source", {})),
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
        if digest != content_sha256(RecipeDefinition.model_validate(resolved.document)):
            raise WorkloadRunWorkflowError(
                "catalog.digest_mismatch", "catalog recipe digest is inconsistent"
            )
        now = self._clock()
        with self._sessions.begin() as session:
            current = session.scalar(
                select(CatalogDocumentRevision)
                .where(
                    CatalogDocumentRevision.document_id == recipe_id,
                    CatalogDocumentRevision.state == "candidate",
                )
                .order_by(CatalogDocumentRevision.revision_number.desc())
                .limit(1)
            )
            if current is None or current.id != snapshot_id:
                raise WorkloadRunWorkflowError(
                    "catalog.stale_revision", "WorkloadRun draft revision changed"
                )
            service = CatalogEntityService(session, clock=self._clock)
            service.fail_candidate(recipe_id)
            next_revision = service.revise(
                recipe_id,
                resolved.document,
                actor=actor,
                expected_revision=expected_revision,
            )
            next_revision = service.resolve(next_revision.id, actor=actor)
            _record_bundle(session, resolved.bundle, stored_bundle.archive_bytes, now)
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
