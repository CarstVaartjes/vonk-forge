"""Artifact inspection."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session
from vonk_agent_protocol import (
    ModelFileState,
    RunSwitchCode,
)

from ..model_cache import ModelCacheService
from ..model_cache_contract import ModelCacheDownloadPreviewResponse
from ..models import (
    NodeArtifact,
)
from ..run_switch_contract import (
    RunSwitchReason,
)
from .interfaces import ArtifactInspection
from .planning_helpers import _as_reason, _inspection_unavailable


class DatabaseRunSwitchArtifactInspector:
    """Conservative Spark-side coverage inspector.

    The Controller model-cache provider is the sole authority for NAS
    coverage.  A database row can describe target-local observations, but it
    cannot identify the complete immutable model set or its NAS download
    plan.  Construction without the provider therefore fails explicitly.
    """

    def __init__(self, model_cache: ModelCacheService | None = None) -> None:
        self._model_cache = model_cache

    def bind_model_cache(self, model_cache: ModelCacheService) -> None:
        """Attach the Controller model-cache authority after startup wiring."""

        self._model_cache = model_cache

    def inspect(
        self,
        session: Session,
        *,
        model_content_sha256: str,
        recipe_revision_id: str,
        node_ids: tuple[str, ...],
        retention: str,
        now: datetime,
    ) -> ArtifactInspection:
        if self._model_cache is None:
            return _inspection_unavailable(
                "model-cache manifest provider is unavailable"
            )
        return self._inspect_model_cache(
            session,
            model_cache=self._model_cache,
            model_content_sha256=model_content_sha256,
            recipe_revision_id=recipe_revision_id,
            node_ids=node_ids,
            retention=retention,
            now=now,
        )

    def _inspect_model_cache(
        self,
        session: Session,
        *,
        model_cache: ModelCacheService,
        model_content_sha256: str,
        recipe_revision_id: str,
        node_ids: tuple[str, ...],
        retention: str,
        now: datetime,
    ) -> ArtifactInspection:
        """Read exact model identity from the cache manifest provider.

        Resolution is metadata-only.  A partial NAS set is a planned download
        when the trusted catalog manifest resolves; only an unavailable or
        contradictory provider becomes a blocker.
        """

        try:
            manifest = model_cache.resolve_artifact_set(
                model_content_sha256=model_content_sha256,
                recipe_revision_id=recipe_revision_id,
            )
            preview_document = model_cache.download_preview(
                model_content_sha256=model_content_sha256,
                recipe_revision_id=recipe_revision_id,
            )
            preview = ModelCacheDownloadPreviewResponse.model_validate(
                {
                    key: value
                    for key, value in preview_document.items()
                    if not key.startswith("_")
                }
            )
        except (OSError, RuntimeError, TypeError, ValueError, KeyError) as error:
            return _inspection_unavailable(
                f"model-cache exact manifest is unavailable: {error}"
            )
        artifact_set_sha256 = manifest.digest
        if preview.artifact_set_sha256 != artifact_set_sha256:
            return _inspection_unavailable(
                "model-cache download preview does not match its manifest"
            )
        if manifest.model_content_sha256 != model_content_sha256:
            return _inspection_unavailable(
                "model-cache manifest model identity does not match the request"
            )
        # The cache authority validates the manifest.  Consume its exact typed
        # fields, including empty support files and shared physical objects.
        expected_by_digest = {
            artifact.sha256: artifact.expected_bytes for artifact in manifest.artifacts
        }
        model_digests = tuple(expected_by_digest)
        artifact_bytes = manifest.expected_bytes
        # Transfer checkpoints reduce the bytes left to download, but do not
        # make an object verified or available for profile admission.
        missing_nas_bytes = (
            sum(expected_by_digest.values()) - preview.already_cached_bytes
        )
        blockers = [
            _as_reason(
                RunSwitchCode.NAS_DOWNLOAD_BLOCKED,
                detail,
                scope="artifact",
                node_ids=node_ids,
            )
            for detail in preview.blockers
        ]
        reused = 0
        missing_spark = 0
        missing_by_node: dict[str, int] = {}
        reclaimable = 0
        reclaimable_digests: set[str] = set()
        for node_id in node_ids:
            missing_by_node[node_id] = 0
            rows = tuple(
                session.scalars(
                    select(NodeArtifact).where(NodeArtifact.node_id == node_id)
                )
            )
            by_digest = {row.digest: row for row in rows}
            for digest, size in expected_by_digest.items():
                row = by_digest.get(digest)
                if (
                    row is not None
                    and row.state == ModelFileState.VERIFIED
                    and row.size_bytes == size
                ):
                    reused += size
                else:
                    missing_spark += size
                    missing_by_node[node_id] += size
            if retention == "reclaim-unreferenced":
                for row in rows:
                    if (
                        row.digest in expected_by_digest
                        and row.state == ModelFileState.VERIFIED
                        and row.ref_count == 0
                    ):
                        reclaimable += row.size_bytes
                        reclaimable_digests.add(row.digest)
        warnings: list[RunSwitchReason] = []
        if missing_nas_bytes:
            warnings.append(
                _as_reason(
                    RunSwitchCode.NAS_DOWNLOAD_REQUIRED,
                    "The exact model artifact set is resolved but missing from the NAS cache; the operation will download it before Spark transfer.",
                    scope="artifact",
                    severity="warning",
                    node_ids=node_ids,
                )
            )
        dependencies = tuple(
            sorted(set(manifest.model_content_digests) - {model_content_sha256})
        )
        return ArtifactInspection(
            required_bytes=artifact_bytes * len(node_ids),
            reused_bytes=reused,
            copied_bytes=missing_spark,
            missing_nas_bytes=missing_nas_bytes,
            missing_spark_bytes=missing_spark,
            missing_spark_bytes_by_node=missing_by_node,
            reclaimable_bytes=reclaimable,
            nas_coverage="complete" if missing_nas_bytes == 0 else "partial",
            spark_coverage="complete" if missing_spark == 0 else "partial",
            artifact_digests=tuple(model_digests),
            reclaimable_digests=tuple(sorted(reclaimable_digests)),
            freshness=(),
            blockers=tuple(blockers),
            warnings=tuple(warnings),
            artifact_set_sha256=artifact_set_sha256,
            artifact_set_bytes=artifact_bytes,
            dependency_model_content_sha256=dependencies,
        )
