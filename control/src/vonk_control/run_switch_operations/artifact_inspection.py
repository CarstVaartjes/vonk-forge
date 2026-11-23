"""Artifact inspection."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from typing import NamedTuple

from sqlalchemy import select
from sqlalchemy.orm import Session, load_only
from vonk_agent_protocol.agent_words import (
    ProfileChildJobKind,
    ProfileReasonSeverity,
    RunSwitchReasonScopeName,
)
from vonk_agent_protocol.lifecycle_vocabulary import LifecycleState, WaitReason
from vonk_agent_protocol.reason_codes.runs import RunSwitchCode
from vonk_agent_protocol.state_machines import PlacementInstallState

from ..categorized_errors import BookkeepingUnknown
from ..inventory.repository import INVENTORY_MAX_AGE, spark_model_objects
from ..lifecycle.evidence import Residue
from ..model_cache.artifacts import ArtifactSetManifest
from ..model_cache.contract import ModelCacheDownloadPreviewResponse
from ..model_cache.service import ModelCacheService
from ..models.jobs import Job
from ..run_switch_contract.evidence import RunSwitchReason
from ..stored.json_values import read_row_column
from .interfaces import ArtifactInspection
from .planning_helpers import (
    _as_reason,
    _aware,
    _inspection_unavailable,
    _stored_job_plan,
)
from .result_helpers import _read_progress


def spark_stores(
    session: Session,
    node_ids: Iterable[str],
    *,
    now: datetime,
    floors: Mapping[str, datetime] | None = None,
) -> dict[str, Mapping[str, int] | None]:
    """Each Spark's own store scan, None where it could not be read or was
    received at or before that Spark's floor."""

    return {
        node_id: spark_model_objects(
            session,
            node_id,
            now=now,
            received_after=None if floors is None else floors.get(node_id),
        )
        for node_id in node_ids
    }


def missing_spark_bytes_by_node(
    session: Session,
    manifest: ArtifactSetManifest,
    node_ids: Iterable[str],
    *,
    now: datetime,
) -> dict[str, int]:
    """Bytes of a model set each Spark's store does not hold.

    From the agent's own store scan; unknown counts as missing, and the copy it
    then plans reuses what the Spark already holds.
    """

    nodes = tuple(node_ids)
    floors = _scan_floors(session, manifest, nodes, now)
    return missing_spark_bytes(
        manifest, spark_stores(session, nodes, now=now, floors=floors)
    )


def _scan_floors(
    session: Session,
    manifest: ArtifactSetManifest,
    node_ids: tuple[str, ...],
    now: datetime,
) -> dict[str, datetime]:
    """Per node, the time at or before which its store scan no longer counts.

    A target copy that failed (ended, or being re-planned) found the files
    gone, so a scan received before it can still list them: the plan for the
    same node and model plans the copy until a newer scan arrives. Only an end
    younger than a scan can be is read; an older scan is already unknown.
    """

    floors: dict[str, datetime] = {}
    wanted = set(node_ids)
    if not wanted:
        return floors
    for copy in _failed_target_copies(session, now):
        if copy.model_content_sha256 != manifest.model_content_sha256:
            continue
        for node_id in wanted.intersection(copy.targets):
            floors[node_id] = max(copy.failed_at, floors.get(node_id, copy.failed_at))
    return floors


class _FailedCopy(NamedTuple):
    targets: frozenset[str]
    failed_at: datetime
    model_content_sha256: str | None


_FAILED_COPY_SCOPE: ContextVar[list[tuple[_FailedCopy, ...]] | None] = ContextVar(
    "failed_copy_read_scope", default=None
)


@contextmanager
def failed_copies_read_once() -> Iterator[None]:
    """Read the recent failed target copies once inside this read.

    The scan is independent of the model, so a pass that inspects many
    candidates (the library) runs one jobs query, not one per candidate.
    """

    token = _FAILED_COPY_SCOPE.set([])
    try:
        yield
    finally:
        _FAILED_COPY_SCOPE.reset(token)


def _failed_target_copies(session: Session, now: datetime) -> tuple[_FailedCopy, ...]:
    """Recent target copies that failed or are being re-planned, per node set."""

    scope = _FAILED_COPY_SCOPE.get()
    if scope:
        return scope[0]
    copies: list[_FailedCopy] = []
    for job in session.scalars(
        select(Job)
        .options(
            load_only(Job.state, Job.targets, Job.updated_at, Job.result, Job.payload)
        )
        .where(
            Job.kind == ProfileChildJobKind.RUN_SWITCH.value,
            Job.updated_at >= now - INVENTORY_MAX_AGE,
        )
    ):
        result = read_row_column(job, "result")
        if not job.targets or isinstance(result, Residue):
            continue
        progress = _read_progress(result)
        if progress.subphase != "target-copy" or not (
            job.state == LifecycleState.FAILED.value or progress.force_replan
        ):
            continue
        plan = _stored_job_plan(job)
        if plan is None:
            continue
        copies.append(
            _FailedCopy(
                frozenset(job.targets),
                _aware(job.updated_at),
                plan.model_content_sha256,
            )
        )
    found = tuple(copies)
    if scope is not None:
        scope.append(found)
    return found


def missing_spark_bytes(
    manifest: ArtifactSetManifest,
    held_by_node: Mapping[str, Mapping[str, int] | None],
) -> dict[str, int]:
    """Bytes of a model set each Spark lacks, from store scans already read."""

    expected_by_digest = {
        artifact.sha256: artifact.expected_bytes for artifact in manifest.artifacts
    }
    return {
        node_id: sum(
            size
            for digest, size in expected_by_digest.items()
            if (held or {}).get(digest) != size
        )
        for node_id, held in held_by_node.items()
    }


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
        except (OSError, RuntimeError, TypeError, ValueError, KeyError) as error:
            return _inspection_unavailable(
                f"model-cache exact manifest is unavailable: {error}"
            )
        warnings: list[RunSwitchReason] = []
        preview: ModelCacheDownloadPreviewResponse | None = None
        try:
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
        except BookkeepingUnknown as error:
            if error.typed_reason is not WaitReason.OBSERVATION_UNAVAILABLE:
                raise
            # Identity is resolved; only availability is pending. Preserve the
            # exact set so the ordinary preparation phase can observe it.
            warnings.append(
                _as_reason(
                    RunSwitchCode.NAS_COVERAGE_UNKNOWN,
                    str(error),
                    scope=RunSwitchReasonScopeName.ARTIFACT.value,
                    severity=ProfileReasonSeverity.WARNING.value,
                    node_ids=node_ids,
                )
            )
        except (OSError, RuntimeError, TypeError, ValueError, KeyError) as error:
            return _inspection_unavailable(
                f"model-cache download preview is unavailable: {error}"
            )
        artifact_set_sha256 = manifest.digest
        if preview is not None and preview.artifact_set_sha256 != artifact_set_sha256:
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
            None
            if preview is None
            else sum(expected_by_digest.values()) - preview.already_cached_bytes
        )
        blockers = [
            _as_reason(
                RunSwitchCode.NAS_DOWNLOAD_BLOCKED,
                detail,
                scope="artifact",
                node_ids=node_ids,
            )
            for detail in (() if preview is None else preview.blockers)
        ]
        missing_by_node = missing_spark_bytes_by_node(
            session, manifest, node_ids, now=now
        )
        missing_spark = sum(missing_by_node.values())
        reused = sum(expected_by_digest.values()) * len(node_ids) - missing_spark
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
            nas_coverage=(
                PlacementInstallState.UNKNOWN.value
                if preview is None
                else PlacementInstallState.COMPLETE.value
                if missing_nas_bytes == 0
                else PlacementInstallState.PARTIAL.value
            ),
            spark_coverage="complete" if missing_spark == 0 else "partial",
            artifact_count=len(model_digests),
            freshness=(),
            blockers=tuple(blockers),
            warnings=tuple(warnings),
            artifact_set_sha256=artifact_set_sha256,
            artifact_set_bytes=artifact_bytes,
            dependency_model_content_sha256=dependencies,
        )
