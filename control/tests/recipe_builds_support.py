"""Shared helpers for the recipe_builds tests."""

from __future__ import annotations

import hashlib
import io
import json
from datetime import UTC, datetime, timedelta
from importlib import resources
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol.claims import AgentRuntimeIdentity
from vonk_agent_protocol.contracts import canonical_message
from vonk_control.catalog.entities import CatalogEntityService
from vonk_control.inventory.repository import (
    InventoryRepository,
    NodeInventoryReport,
)
from vonk_control.model_primitives import Base
from vonk_control.models.catalog import CatalogDocumentRevision, RecipeSourceBundle
from vonk_control.models.fleet import AgentNode, AgentNodeContact
from vonk_control.models.jobs import Job
from vonk_control.models.operations import AgentOperation
from vonk_control.runtime.adapters import resolve_runtime_adapter
from vonk_control.runtime_image_preparation.contracts import (
    RuntimeImageReceipt,
)
from vonk_control.runtime_image_preparation.storage import FilesystemRuntimeImageStorage
from vonk_control.source_bundles import SourceBundleStore, generate_source_bundle

from .display_fixtures import create_draft
from .runtime_identity_support import (
    PACKAGED_RUNTIME_IDENTITY,
    inventory_items_fixture,
    set_inventory_identity,
)
from .runtime_image_fixtures import place_test_image

_CACHED_ADAPTER = resolve_runtime_adapter("vllm", {"node_count": 1})


class RecordingQueue:
    def enqueue_in_session(
        self,
        session: Session,
        parent_job_id: str,
        node_id: str,
        operation: str,
        authority_revision: str,
        payload: object,
        *,
        operation_id: str,
    ) -> AgentOperation:
        parent = session.get(Job, parent_job_id)
        assert parent is not None
        record = AgentOperation(
            id=operation_id,
            parent_job_id=parent_job_id,
            node_id=node_id,
            kind=operation,
            payload_digest="f" * 64,
            payload=json.loads(canonical_message(payload)),
            authority_revision=authority_revision,
            workload_intent_ordinal=parent.payload.get("workload_intent_ordinal"),
            state="queued",
            current_attempt=0,
            created_at=datetime(2026, 8, 7, 12, tzinfo=UTC),
            updated_at=datetime(2026, 8, 7, 12, tzinfo=UTC),
        )
        session.add(record)
        return record

    def notify_available(self) -> None:
        pass


def _json_object(value: object) -> dict[str, object]:
    """Narrow decoded JSON to a mutable object; a wrong shape fails the test."""

    assert isinstance(value, dict)
    return value


def _json_array(value: object) -> list[object]:
    """Narrow decoded JSON to a mutable array; a wrong shape fails the test."""

    assert isinstance(value, list)
    return value


def _json_text(value: object) -> str:
    """Narrow decoded JSON to a string; a wrong shape fails the test."""

    assert isinstance(value, str)
    return value


def _restore_builder_inventory(sessions, node_id, now):
    InventoryRepository(sessions, clock=lambda: now).record(
        NodeInventoryReport.model_validate_json(
            canonical_message(
                {
                    "node_id": node_id,
                    "observed_at": now,
                    "disk_total_bytes": 2 * 1024**4,
                    "disk_free_bytes": 1 * 1024**4,
                    "host_memory_total_bytes": 100_000,
                    "host_memory_free_bytes": 80_000,
                    "gpu_memory_total_bytes": 100_000,
                    "gpu_memory_free_bytes": 80_000,
                    "gpu_count": 1,
                    "artifact_store_read_only": False,
                    "capabilities": ("recipe.build.v1", "recipe.build.egress-proxy.v1"),
                    "memory_pool": "separate",
                    "schema_version": 1,
                    "nvidia_driver_version": "unknown",
                    "container_runtime_version": "unknown",
                }
            )
        )
    )


def setup(
    tmp_path: Path,
    *,
    network: dict[str, object] | None = None,
    engine=None,
    existing_node: bool = False,
    builder_node_id: str | None = None,
    recipe_slug: str = "qwen3-vllm",
):
    engine = engine or create_engine(f"sqlite:///{tmp_path / 'build.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 8, 7, 12, tzinfo=UTC)
    if existing_node:
        now += timedelta(seconds=1)
    node_id = builder_node_id or "spk_" + "1" * 32
    bundle = generate_source_bundle(
        {
            "Dockerfile": (
                "FROM ghcr.io/example/vllm@sha256:" + "a" * 64 + "\nUSER 10001:10001\n"
            ).encode()
        }
    )
    bundles = SourceBundleStore(tmp_path / "bundles")
    stored = bundles.put(bundle.sha256, io.BytesIO(bundle.archive))
    document = json.loads(
        resources.files("vonk_forge_contracts")
        .joinpath("examples", "recipe-source-build.json")
        .read_text(encoding="utf-8")
    )
    document["execution"]["build"]["network"] = network or {"hosts": []}
    document["identity"]["slug"] = recipe_slug
    with sessions.begin() as session:
        builder = session.get(AgentNode, node_id) if existing_node else None
        if existing_node:
            assert builder is not None
            set_inventory_identity(builder, binary_digest="1" * 64)
        else:
            session.add(
                AgentNode(
                    node_id=node_id,
                    state="active",
                    inventory_snapshot=inventory_items_fixture(
                        node_id,
                        observed_at=now,
                        identity=AgentRuntimeIdentity.model_validate(
                            {
                                **PACKAGED_RUNTIME_IDENTITY,
                                "architecture": "linux-arm64",
                                "semantic_version": "1.2.3",
                                "build_digest": "sha256:" + "a" * 64,
                                "binary_digest": "1" * 64,
                            }
                        ),
                    ),
                    contact=AgentNodeContact(last_seen_at=now),
                )
            )
        # Another recipe on an existing builder may share the same bundle.
        session.merge(
            RecipeSourceBundle(
                sha256=bundle.sha256,
                media_type="application/vnd.vonk-forge.source-bundle.v1+tar",
                archive_bytes=stored.archive_bytes,
                total_bytes=bundle.manifest.total_bytes,
                file_count=len(bundle.manifest.files),
                storage_key=f"{bundle.sha256[:2]}/{bundle.sha256}.tar",
                manifest=bundle.manifest.model_dump(mode="json"),
                verified_at=now,
            )
        )
    InventoryRepository(sessions, clock=lambda: now).record(
        NodeInventoryReport.model_validate_json(
            canonical_message(
                {
                    "node_id": node_id,
                    "observed_at": now,
                    "disk_total_bytes": 2 * 1024**4,
                    "disk_free_bytes": 1 * 1024**4,
                    "host_memory_total_bytes": 100_000,
                    "host_memory_free_bytes": 80_000,
                    "gpu_memory_total_bytes": 100_000,
                    "gpu_memory_free_bytes": 80_000,
                    "gpu_count": 1,
                    "artifact_store_read_only": False,
                    "capabilities": (
                        "recipe.build.v1",
                        "recipe.build.egress-proxy.v1",
                        "recipe.image.import.v1",
                        "runtime.vonk.v1",
                        "recipe.image.pull.v1",
                    ),
                    "memory_pool": "separate",
                    "schema_version": 1,
                    "nvidia_driver_version": "unknown",
                    "container_runtime_version": "unknown",
                }
            )
        )
    )
    catalog = CatalogEntityService(sessions, clock=lambda: now)
    model = json.loads(
        resources.files("vonk_forge_contracts")
        .joinpath("examples", "model-definition.json")
        .read_text(encoding="utf-8")
    )
    if not existing_node:
        model_draft = create_draft(catalog, model, actor="admin")
        catalog.resolve(model_draft.id, actor="admin")
    recipe_draft = create_draft(catalog, document, actor="admin")
    with sessions.begin() as session:
        stored_revision = session.get(CatalogDocumentRevision, recipe_draft.id)
        assert stored_revision is not None
        stored_revision.source_bundle_sha256 = bundle.sha256
        stored_revision.projected = {
            **stored_revision.projected,
            "build_resources": {
                "cpu_cores": 6,
                "download_bytes": 100,
                "temporary_bytes": 200,
                "memory_bytes": 300,
                "processes": 2048,
                "timeout_seconds": 600,
            },
            "build_security": {"capabilities": ["DAC_OVERRIDE"]},
            "build_options": {
                "additional_contexts": [],
                "annotations": [],
                "environment": [],
                "format": "oci",
                "identity_label": True,
                "ignorefile": None,
                "jobs": 1,
                "labels": [],
                "layer_compression": "disabled",
                "layer_labels": [],
                "layers": True,
                "no_hostname": False,
                "no_hosts": False,
                "omit_history": False,
                "os_features": [],
                "os_version": None,
                "shm_bytes": 67108864,
                "skip_unused_stages": True,
                "squash": "none",
                "timestamp": None,
                "unset_environment": [],
                "unset_labels": [],
            },
            "build_model_artifacts": [
                {
                    "path": "model.safetensors",
                    "sha256": "c" * 64,
                    "size_bytes": 1024,
                }
            ],
        }
    revision = catalog.resolve(recipe_draft.id, actor="admin")
    return sessions, bundles, now, node_id, revision


def _write_controller_build_receipt(
    storage: FilesystemRuntimeImageStorage,
    *,
    archive: bytes,
    image_digest: str,
    build_id: str,
    build_input_sha256: str,
    distribution_content_sha256: str,
) -> RuntimeImageReceipt:
    """Publish the exact filesystem receipt a completed Controller build leaves."""

    digest = hashlib.sha256(archive).hexdigest()
    place_test_image(storage, digest, len(archive))
    staged = storage.existing_archive(digest, len(archive))
    return storage.commit(
        staged,
        receipt=RuntimeImageReceipt(
            schema_version=2,
            distribution_publisher="vonk",
            distribution_slug="cached",
            distribution_content_sha256=distribution_content_sha256,
            image_digest=image_digest,
            oci_archive_sha256=hashlib.sha256(archive).hexdigest(),
            image_bytes=len(archive),
            local_image_config_id="sha256:" + "c" * 64,
            architecture="linux-arm64",
            runtime_interface="vonk.runtime.v1",
            archive_path=str(staged),
            recorded_at="2026-09-15T00:00:00Z",
            build_id=build_id,
            build_input_sha256=build_input_sha256,
            runtime_interface_label="v1",
            runtime_adapter=_CACHED_ADAPTER.adapter_id,
            runtime_adapter_sha256=_CACHED_ADAPTER.digest,
        ),
    )
