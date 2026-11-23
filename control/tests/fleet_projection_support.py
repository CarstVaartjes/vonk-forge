from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from importlib.resources import files

from vonk_agent_protocol.telemetry import HostResourceSample
from vonk_control.models import (
    AgentCertificate,
    AgentNodeProfile,
    CatalogDocument,
    CatalogDocumentRevision,
    NodeInventorySnapshot,
    NodeTelemetrySample,
)
from vonk_forge_contracts import ModelDefinition, RecipeDefinition, document_sha256

from .telemetry_support import telemetry_with_resources

NOW = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)


NODE_A = "spk_" + "1" * 32


NODE_B = "spk_" + "2" * 32


def _canonical_catalog_documents(
    recipe_id: str,
    revision_id: str,
    model_id: str,
    model_revision_id: str,
    *,
    slug: str,
    title: str,
    interfaces: list[dict[str, object]] | None = None,
) -> tuple[list[CatalogDocument], list[CatalogDocumentRevision]]:
    model = ModelDefinition.model_validate(
        json.loads(
            files("vonk_forge_contracts")
            .joinpath("examples", "model-definition.json")
            .read_text(encoding="utf-8")
        )
    )
    recipe_document = json.loads(
        files("vonk_forge_contracts")
        .joinpath("examples", "recipe-source-build.json")
        .read_text(encoding="utf-8")
    )
    recipe_document["identity"]["slug"] = slug
    recipe_document["models"][0]["model"]["content_sha256"] = document_sha256(
        model.model_dump(mode="json")
    )
    if interfaces is not None:
        recipe_document["interfaces"] = interfaces
    recipe = RecipeDefinition.model_validate(recipe_document)
    model_document = model.model_dump(mode="json")
    canonical_recipe = recipe.model_dump(mode="json")
    return (
        [
            CatalogDocument(
                id=recipe_id,
                kind="recipe",
                publisher=recipe.identity.publisher,
                slug=recipe.identity.slug,
                title=title,
                created_by="admin",
                created_at=NOW,
                updated_at=NOW,
            ),
            CatalogDocument(
                id=model_id,
                kind="model",
                publisher=model.identity.publisher,
                slug=model.identity.slug,
                title=model.identity.slug,
                created_by="admin",
                created_at=NOW,
                updated_at=NOW,
            ),
        ],
        [
            CatalogDocumentRevision(
                id=revision_id,
                document_id=recipe_id,
                kind="recipe",
                publisher=recipe.identity.publisher,
                slug=recipe.identity.slug,
                revision_number=1,
                schema_version=2,
                state="active",
                document=canonical_recipe,
                content_digest=document_sha256(recipe.model_dump(mode="json")),
                projected={},
                created_by="admin",
                created_at=NOW,
            ),
            CatalogDocumentRevision(
                id=model_revision_id,
                document_id=model_id,
                kind="model",
                publisher=model.identity.publisher,
                slug=model.identity.slug,
                revision_number=1,
                schema_version=2,
                state="active",
                document=model_document,
                content_digest=document_sha256(model.model_dump(mode="json")),
                projected={},
                created_by="admin",
                created_at=NOW,
            ),
        ],
    )


def _inventory(
    node_id: str, observed_at: datetime, *, free_bytes: int
) -> NodeInventorySnapshot:
    suffix = node_id.removeprefix("spk_")[0]
    return NodeInventorySnapshot(
        id=f"00000000-0000-4000-8000-{free_bytes:012d}",
        node_id=node_id,
        observed_at=observed_at,
        received_at=observed_at + timedelta(seconds=1),
        disk_total_bytes=1_000,
        disk_free_bytes=free_bytes,
        host_memory_total_bytes=2_000,
        host_memory_free_bytes=1_500,
        gpu_memory_total_bytes=2_000,
        gpu_memory_free_bytes=1_400,
        gpu_count=1,
        fabric_address=f"10.0.0.{int(suffix, 16)}",
        fabric_bandwidth_mbps=100_000,
        nvidia_driver_version="580.1",
        container_runtime_version="1.2.3",
        artifact_store_read_only=False,
        capabilities=["runtime.vonk.v1", "recipe.image.pull.v1"],
        evidence_digest=((suffix + f"{free_bytes:x}") * 64)[:64],
        memory_pool="separate",
    )


def _telemetry(
    node_id: str,
    sample_id: str,
    observed_at: datetime,
    *,
    sequence: int,
    cpu: float,
    boot_id: str = "00000000-0000-4000-8000-000000000001",
) -> NodeTelemetrySample:
    return telemetry_with_resources(
        NodeTelemetrySample(
            id=sample_id,
            node_id=node_id,
            boot_id=boot_id,
            observed_at=observed_at,
            received_at=observed_at + timedelta(milliseconds=250),
            gpu_utilization_percent=cpu,
            gpu_memory_total_bytes=2_000,
            gpu_memory_free_bytes=1_300,
        ),
        HostResourceSample(
            memory_total_bytes=2_000,
            memory_available_bytes=1_400,
            disk_total_bytes=1_000,
            disk_free_bytes=700,
        ),
    )


def _certificate(
    node_id: str,
    serial: str,
    *,
    generation: int = 1,
    state: str = "active",
    not_before: datetime = NOW - timedelta(days=1),
    not_after: datetime = NOW + timedelta(days=1),
    revoked_at: datetime | None = None,
    ca_revoked_at: datetime | None = None,
) -> AgentCertificate:
    return AgentCertificate(
        serial=serial,
        node_id=node_id,
        not_before=not_before,
        not_after=not_after,
        fingerprint=f"fingerprint-{serial}",
        state=state,
        generation=generation,
        revoked_at=revoked_at,
        ca_revoked_at=ca_revoked_at,
    )


def _profile(
    node_id: str,
    *,
    display_name: str,
    lifecycle: str = "managed",
    labels: dict[str, str] | None = None,
) -> AgentNodeProfile:
    return AgentNodeProfile(
        node_id=node_id,
        display_name=display_name,
        lifecycle=lifecycle,
        labels={} if labels is None else labels,
    )
