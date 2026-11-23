"""Shared helpers for the recipe_image_availability tests."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from sqlalchemy import create_engine, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker
from vonk_forge_contracts import RecipeDefinition, document_sha256

from vonk_agent_protocol.wire_model import OperationProgress
from vonk_control.catalog.revision_contract import (
    recipe_document_projection,
    write_catalog_projection,
)
from vonk_control.jobs.documents import (
    AvailabilityJobPayload,
    AvailabilityRetry,
    AvailabilityRuntime,
)
from vonk_control.model_cache.contract import (
    CacheManifest,
    ModelCacheDownloadPayload,
    ModelCacheDownloadPreviewResponse,
    ModelCacheOperationProgress,
)
from vonk_control.model_primitives import Base
from vonk_control.models.cache import (
    ModelCacheOperation,
    ModelCacheSet,
    RecipeBuild,
)
from vonk_control.models.catalog import (
    CatalogDocument,
    CatalogDocumentHead,
    CatalogDocumentRevision,
)
from vonk_control.models.fleet import (
    AgentNode,
)
from vonk_control.recipe_image_availability.service import (
    RecipeImageAvailabilityService,
)
from vonk_control.recipe_operations.availability_intent import RecipeRevisionIntent
from vonk_control.runtime_image_preparation.contracts import (
    PulledImageEvidence,
    RuntimeImageReceipt,
)
from vonk_control.runtime_image_preparation.storage import FilesystemRuntimeImageStorage
from vonk_control.strict_json import serialize_json_value

from .display_fixtures import reworded
from .runtime_image_fixtures import place_test_image

IMAGE_DIGEST = "sha256:" + "d" * 64


CONFIG_DIGEST = "sha256:" + "c" * 64


ARCHIVE = b"availability image archive"


ARCHIVE_SHA = hashlib.sha256(ARCHIVE).hexdigest()


def _recipe(name: str) -> RecipeDefinition:
    raw = json.loads(
        files("vonk_forge_contracts").joinpath("examples", name).read_text()
    )
    return RecipeDefinition.model_validate(raw)


def _published(recipe: RecipeDefinition) -> dict[str, Any]:
    """The document as the catalog publishes and stores it: only what was set.

    Its digest is the one ingress verifies. A full ``model_dump()`` adds the
    defaults the published document leaves out and hashes differently.
    """

    return recipe.model_dump(mode="json", exclude_unset=True)


def _download_preview(*, artifact_set_sha256: str, plan_digest: str, new_bytes: int):
    return ModelCacheDownloadPreviewResponse(
        artifact_set_sha256=artifact_set_sha256,
        plan_digest=plan_digest,
        artifact_count=0,
        expected_bytes=new_bytes,
        already_cached_bytes=0,
        new_bytes=new_bytes,
        blockers=[],
        warnings=[],
    ).model_dump(mode="json")


def _manifest(*, model_content_digests: list[str]):
    return CacheManifest(
        schema_version=2,
        source_policy="nas-first",
        model_content_sha256=None,
        recipe_revision_sha256=None,
        model_definition_ref=None,
        model_content_digests=model_content_digests,
        artifacts=[],
    ).model_dump(mode="json")


def _runtime() -> dict[str, object]:
    return {
        "architecture": "linux/arm64",
        "interface": "vonk.runtime.v1",
        "image_bytes": len(ARCHIVE),
        "build_input_sha256": "f" * 64,
    }


def _availability_payload(revision_id: str) -> AvailabilityJobPayload:
    recipe = _recipe("recipe-source-build.json")
    return AvailabilityJobPayload(
        schema_version=2,
        kind="recipe.image.availability.v2",
        request=RecipeRevisionIntent(recipe_revision_id=revision_id),
        recipe_revision_id=revision_id,
        recipe_content_sha256=document_sha256(_published(recipe)),
        recipe=recipe,
        runtime=AvailabilityRuntime.model_validate(_runtime()),
        force_rebuild=False,
        progress=OperationProgress(phase="prepare"),
        retry=AvailabilityRetry(automatic_attempts=0, operator_retries=0),
    )


def _typed_availability_payload(
    observations: dict[str, object], recipe: RecipeDefinition
):
    """Behavior fixtures carry the same complete intent the service writes."""

    revision_id = str(observations["recipe_revision_id"])
    base = _availability_payload(revision_id).model_dump(mode="json")
    base["recipe"] = recipe.model_dump(mode="json")
    base["recipe_content_sha256"] = document_sha256(_published(recipe))
    runtime = base["runtime"] | observations.get("runtime", {})
    return serialize_json_value(
        AvailabilityJobPayload.model_validate_json(
            json.dumps(base | observations | {"runtime": runtime})
        )
    )


_build_runtime = _runtime


def _build_id(revision_id: str) -> str:
    """The succeeded build ``_add_revision`` records for one revision."""

    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"vonk-test-build:{revision_id}"))


def _builder(
    storage: Any,
    *,
    payload: bytes = ARCHIVE,
    calls: list[bool] | None = None,
):
    """A builder that leaves the revision's recorded build archive in storage."""

    def build(*_args: object, claim: Any, force: bool = False, **_: object):
        if calls is not None:
            calls.append(force)
        digest = hashlib.sha256(payload).hexdigest()
        place_test_image(storage, digest, len(payload))
        return {
            "state": "succeeded",
            "build_id": _build_id(str(claim.recipe_revision_id)),
            "build_input_sha256": "f" * 64,
            "image_digest": IMAGE_DIGEST,
            "oci_layout_sha256": digest,
            "image_bytes": len(payload),
        }

    return build


def _service(*args: object, **kwargs: object) -> RecipeImageAvailabilityService:
    """The availability service with the test builder unless one is given."""

    storage = kwargs["storage"]
    assert isinstance(storage, FilesystemRuntimeImageStorage)
    kwargs.setdefault("builder", _builder(storage))
    kwargs.setdefault("transport", Transport())
    return RecipeImageAvailabilityService(*args, **kwargs)  # type: ignore[arg-type]


def _reference_receipt(
    build_id: str = "00000000-0000-4000-8000-000000000999",
) -> RuntimeImageReceipt:
    return RuntimeImageReceipt(
        schema_version=2,
        distribution_publisher="test-publisher",
        distribution_slug="test-image",
        distribution_content_sha256="a" * 64,
        image_digest=IMAGE_DIGEST,
        oci_archive_sha256=ARCHIVE_SHA,
        image_bytes=len(ARCHIVE),
        local_image_config_id=CONFIG_DIGEST,
        architecture="linux-arm64",
        runtime_interface="vonk.runtime.v1",
        runtime_interface_label="v1",
        archive_path="/managed/image-cache/" + ARCHIVE_SHA,
        recorded_at=datetime.now(UTC).isoformat(),
        build_id=build_id,
        runtime_adapter="vllm",
        runtime_adapter_sha256="a" * 64,
    )


class Transport:
    """Inspects a stored build archive the way skopeo reports it."""

    def __init__(self) -> None:
        self.calls = 0

    def inspect_archive(
        self,
        archive: Path,
        *,
        expected_architecture: str,
        expected_runtime_interface: str,
        expected_archive_sha256: str,
        expected_archive_bytes: int,
    ) -> PulledImageEvidence:
        self.calls += 1
        assert archive.is_file()
        return PulledImageEvidence(
            manifest_digest=IMAGE_DIGEST,
            config_id=CONFIG_DIGEST,
            local_reference="oci-layout:" + archive.name,
            architecture=expected_architecture,
            runtime_interface=expected_runtime_interface,
            archive_sha256=expected_archive_sha256,
            archive_bytes=expected_archive_bytes,
        )


def _ensure_builder_node(session: Session) -> None:
    """PostgreSQL enforces the build's builder foreign key; SQLite does not."""

    with session.no_autoflush:
        exists = session.get(AgentNode, "spark-builder") is not None or any(
            isinstance(item, AgentNode) and item.node_id == "spark-builder"
            for item in session.new
        )
    if not exists:
        session.add(AgentNode(node_id="spark-builder", state="active"))


def _recipe_projection(recipe: RecipeDefinition) -> dict[str, object]:
    """The stored catalog projection of one recipe."""

    return write_catalog_projection(recipe_document_projection(recipe), kind="recipe")


def _add_revision(
    session: Session,
    revision_id: str,
    recipe: RecipeDefinition,
    *,
    built: bool = True,
    archive: bytes = ARCHIVE,
    document_id: str | None = None,
) -> CatalogDocumentRevision:
    """One active recipe revision and, unless ``built`` is false, its build."""

    revision = CatalogDocumentRevision(
        id=revision_id,
        document_id=document_id or "document-" + revision_id,
        kind="recipe",
        publisher=recipe.identity.publisher,
        slug=recipe.identity.slug,
        revision_number=1,
        schema_version=2,
        state="active",
        document=_published(recipe),
        content_digest=document_sha256(_published(recipe)),
        artifact_key="b" * 64,
        execution_key="a" * 64,
        source_bundle_sha256="b" * 64,
        projected=_recipe_projection(recipe),
        created_by="test",
        created_at=datetime.now(UTC),
    )
    session.add(revision)
    if built:
        _ensure_builder_node(session)
        session.add(
            RecipeBuild(
                id=_build_id(revision_id),
                recipe_revision_id=revision_id,
                builder_node_id="spark-builder",
                source_bundle_sha256="b" * 64,
                build_input_sha256="f" * 64,
                state="succeeded",
                policy_report={},
                plan={},
                image_digest=IMAGE_DIGEST,
                oci_layout_sha256=hashlib.sha256(archive).hexdigest(),
                image_bytes=len(archive),
                error=None,
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )
    return revision


def _persist_fake_model_cache_child(
    sessions: sessionmaker[Session],
    *,
    child: SimpleNamespace,
    model_content_sha256: str,
    now: datetime,
) -> None:
    """Give fake owner responses the same durable identity the service requires."""

    operation_id = child.id
    request_key = child.request_key
    artifact_set_sha256 = child.artifact_set_sha256
    plan_digest = child.plan_digest
    state = child.state
    failure = child.failure
    progress_value = child.progress
    assert isinstance(operation_id, str)
    assert isinstance(request_key, str)
    assert isinstance(artifact_set_sha256, str)
    assert isinstance(plan_digest, str)
    assert state in {"running", "succeeded", "failed"}
    assert isinstance(progress_value, Mapping)
    progress = ModelCacheOperationProgress.model_validate(progress_value)
    expected_bytes = progress.expected_bytes
    assert expected_bytes is not None
    manifest = {
        "schema_version": 2,
        "source_policy": "nas-first",
        "model_content_sha256": model_content_sha256,
        "recipe_revision_sha256": None,
        "model_definition_ref": None,
        "model_content_digests": [model_content_sha256],
        "artifacts": [],
    }
    payload = ModelCacheDownloadPayload.model_validate(
        {
            "schema_version": 2,
            "source_policy": "nas-first",
            "artifact_set_sha256": artifact_set_sha256,
            "manifest": manifest,
            "plan_digest": plan_digest,
            "transfer": {
                "schema_version": 2,
                "total_bytes": expected_bytes,
                "artifacts": {},
            },
            "retry": {"automatic_attempts": 1, "operator_retries": 0},
            "failure": failure,
            "result": (
                {
                    "schema_version": 2,
                    "artifact_set_sha256": artifact_set_sha256,
                    "coverage": "complete",
                }
                if state == "succeeded"
                else None
            ),
        }
    )
    cache_set_state = {
        "running": "downloading",
        "succeeded": "cached",
        "failed": "failed",
    }[state]
    with sessions.begin() as session:
        cache_set = session.get(ModelCacheSet, artifact_set_sha256)
        if cache_set is None:
            session.add(
                ModelCacheSet(
                    artifact_set_sha256=artifact_set_sha256,
                    model_content_sha256=model_content_sha256,
                    recipe_revision_sha256=None,
                    manifest=manifest,
                    expected_bytes=expected_bytes,
                    verified_bytes=min(progress.downloaded_bytes, expected_bytes),
                    state=cache_set_state,
                    created_at=now,
                    updated_at=now,
                    last_accessed_at=now,
                )
            )
        else:
            cache_set.state = cache_set_state
            cache_set.expected_bytes = expected_bytes
            cache_set.verified_bytes = min(progress.downloaded_bytes, expected_bytes)
            cache_set.updated_at = now
        operation = session.get(ModelCacheOperation, operation_id)
        if operation is None:
            operation = ModelCacheOperation(
                id=operation_id,
                request_key=request_key,
                kind="download",
                state=state,
                artifact_set_sha256=artifact_set_sha256,
                plan_digest=plan_digest,
                payload=serialize_json_value(payload),
                progress=progress.model_dump(mode="json"),
                actor="operator",
                created_at=now,
                updated_at=now,
                completed_at=now if state in {"succeeded", "failed"} else None,
            )
            session.add(operation)
        else:
            operation.state = state
            operation.plan_digest = plan_digest
            operation.payload = serialize_json_value(payload)
            operation.progress = progress.model_dump(mode="json")
            operation.updated_at = now
            operation.completed_at = now if state in {"succeeded", "failed"} else None


def _add_head(
    session: Session, revision: CatalogDocumentRevision
) -> CatalogDocumentHead:
    document = CatalogDocument(
        id=revision.document_id,
        kind="recipe",
        publisher=revision.publisher,
        slug=revision.slug,
        title="Recipe",
        created_by="test",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    session.add(document)
    session.flush()
    head = CatalogDocumentHead(
        kind="recipe",
        publisher=revision.publisher,
        slug=revision.slug,
        active_revision_id=revision.id,
        generation=1,
    )
    session.add(head)
    return head


def _add_recipe_successors(
    session: Session,
    *,
    older_id: str,
    older: RecipeDefinition,
    newer_id: str,
    newer: RecipeDefinition,
) -> tuple[CatalogDocumentRevision, CatalogDocumentRevision]:
    """Two revisions of one recipe document with the head on the older one."""

    older_revision = _add_revision(session, older_id, older)
    newer_revision = _add_revision(session, newer_id, newer)
    document_id = older_revision.document_id
    newer_revision.document_id = document_id
    newer_revision.revision_number = 2
    _add_head(session, older_revision)
    return older_revision, newer_revision


def _set_active_head(session: Session, revision_id: str) -> None:
    """Move the authoritative head, as a catalogue sync would."""

    revision = session.get(CatalogDocumentRevision, revision_id)
    assert revision is not None
    head = session.scalar(
        select(CatalogDocumentHead).where(
            CatalogDocumentHead.kind == revision.kind,
            CatalogDocumentHead.publisher == revision.publisher,
            CatalogDocumentHead.slug == revision.slug,
        )
    )
    assert head is not None
    head.active_revision_id = revision_id


def _successor(recipe: RecipeDefinition, title: str) -> RecipeDefinition:
    return reworded(recipe, title)


def _empty_recipe_removal_owner(
    tmp_path: Path,
) -> tuple[
    sessionmaker[Session],
    FilesystemRuntimeImageStorage,
    RecipeImageAvailabilityService,
    str,
]:
    image_recipe = _recipe("recipe-source-build.json")
    job_recipe = _recipe("recipe-job.json")
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine)
    with sessions.begin() as session:
        _add_head(
            session,
            _add_revision(session, "revision-image", image_recipe, built=False),
        )
        _add_head(
            session, _add_revision(session, "revision-job", job_recipe, built=False)
        )
    storage = FilesystemRuntimeImageStorage(tmp_path / "cache")
    service = _service(
        sessions,
        storage=storage,
        authority=lambda *_args, **_kwargs: (image_recipe, _runtime()),
        clock=lambda: datetime.now(UTC),
    )
    return (
        sessions,
        storage,
        service,
        "vonk-forge/synthetic-tiny-build",
    )


def _lock_refused() -> OperationalError:
    origin = Exception(
        'could not obtain lock on row in relation "model_cache_operations"'
    )
    origin.sqlstate = "55P03"  # type: ignore[attr-defined]
    return OperationalError("SELECT 1", {}, origin)
