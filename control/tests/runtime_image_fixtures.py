"""Place runtime images in the Controller's layered store for tests.

The store reads images by name and size only (bytes are verified once, at
ingress), so a test can place an image at the address and size its fixture
already uses instead of producing real registry content.
"""

from __future__ import annotations

import copy as copying
import hashlib
import json
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import select

from vonk_agent_protocol.contracts import canonical_message
from vonk_control.install_admission.identity import _primary_model_sha256
from vonk_control.inventory.repository import (
    InventoryRepository,
    NodeInventoryReport,
)
from vonk_control.models.cache import RecipeBuild
from vonk_control.models.catalog import CatalogDocument, CatalogDocumentRevision
from vonk_control.models.execution import InstallationNode, RecipeInstallation
from vonk_control.models.mapping import ClusterMapping
from vonk_control.models.telemetry import NodeInventorySnapshot
from vonk_control.oci_image_store import OCI_MANIFEST
from vonk_control.runtime_image_preparation.storage import FilesystemRuntimeImageStorage
from vonk_control.strict_json import serialize_json_value

RUNTIME_CONFIG = {
    "architecture": "arm64",
    "os": "linux",
    "config": {
        "User": "10001:10001",
        "Labels": {"ai.vonkforge.runtime-interface": "v1"},
    },
    "rootfs": {"type": "layers", "diff_ids": []},
}


def place_test_image(
    storage: FilesystemRuntimeImageStorage,
    address: str,
    image_bytes: int,
    *,
    config: dict[str, object] | None = None,
) -> None:
    """Store an image at ``address`` (manifest hex) whose layers total ``image_bytes``."""

    blobs = storage.layout.root / "blobs" / "sha256"
    blobs.mkdir(parents=True, exist_ok=True)
    config_bytes = json.dumps(config or RUNTIME_CONFIG, sort_keys=True).encode()
    config_digest = hashlib.sha256(config_bytes).hexdigest()
    (blobs / config_digest).write_bytes(config_bytes)
    layer = hashlib.sha256(f"layer:{address}:{image_bytes}".encode()).hexdigest()
    (blobs / layer).write_bytes(b"\0" * image_bytes)
    manifest = {
        "schemaVersion": 2,
        "mediaType": OCI_MANIFEST,
        "config": {
            "mediaType": "application/vnd.oci.image.config.v1+json",
            "digest": f"sha256:{config_digest}",
            "size": len(config_bytes),
        },
        "layers": [
            {
                "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
                "digest": f"sha256:{layer}",
                "size": image_bytes,
            }
        ],
    }
    (blobs / address).write_bytes(json.dumps(manifest).encode())


def remove_test_image(storage: FilesystemRuntimeImageStorage, address: str) -> None:
    """Simulate cache loss: the image's manifest is gone."""

    (storage.layout.root / "blobs" / "sha256" / address).unlink()


def refresh_inventory(sessions, now) -> None:
    """Publish current physical fixture evidence after advancing an injected clock."""

    with sessions() as session:
        snapshots = {
            row.node_id: row
            for row in session.scalars(
                select(NodeInventorySnapshot).order_by(
                    NodeInventorySnapshot.observed_at
                )
            )
        }
    inventory = InventoryRepository(sessions, clock=lambda: now)
    for snapshot in snapshots.values():
        values = {
            field: getattr(snapshot, field)
            for field in NodeInventoryReport.model_fields
            if field != "schema_version"
        }
        values["observed_at"] = now
        inventory.record(
            NodeInventoryReport.model_validate_json(
                canonical_message(
                    {
                        **values,
                        "schema_version": 1,
                        "nvidia_driver_version": "unknown",
                        "container_runtime_version": "unknown",
                    }
                )
            )
        )


def install_another_image(installation, *, digest: str = "sha256:" + "e" * 64) -> None:
    """Make an installation run another image, as install admission records it.

    The installation row and every compiled rank carry the same image digest.
    """
    plan = json.loads(canonical_message(installation.plan))
    for compiled in plan["compiled_execution_plans"].values():
        compiled["runtime_image"]["image_digest"] = digest
    installation.plan = plan
    installation.image_digest = digest


def install_image_of_replaced_source(
    session, installation, *, digest: str = "sha256:" + "e" * 64
) -> None:
    """Make an installation run an image built from a source bundle its revision
    has since replaced, so a load of the revision needs a new build.

    Its installed ranks keep that image on their Sparks; it is simply not the
    image this revision builds now.
    """

    install_another_image(installation, digest=digest)
    current = session.get(RecipeBuild, installation.recipe_build_id)
    assert current is not None
    previous = RecipeBuild(
        recipe_revision_id=current.recipe_revision_id,
        builder_node_id=current.builder_node_id,
        source_bundle_sha256="f" * 64,
        build_input_sha256="f" * 64,
        state="succeeded",
        policy_report=current.policy_report,
        plan=current.plan,
        image_digest=digest,
        oci_layout_sha256=digest.removeprefix("sha256:"),
        image_bytes=current.image_bytes or 30,
        # Built before the current row, so the current row stays the newest.
        created_at=current.created_at - timedelta(days=1),
        updated_at=current.created_at - timedelta(days=1),
    )
    session.add(previous)
    session.flush()
    installation.recipe_build_id = previous.id


def copy_as_another_recipe(session, revision_id: str, *, model_content_sha256=None):
    """Copy a recipe revision as another recipe (`earlier-model`): the same
    document, so the same image, under another identity, optionally pinning
    another primary model. No profile of the original recipe names it and no
    load of it reuses its installations. Returns the copied revision."""

    revision = session.get(CatalogDocumentRevision, revision_id)
    assert revision is not None
    document = session.get(CatalogDocument, revision.document_id)
    assert document is not None
    other = CatalogDocument(
        **{
            column.name: getattr(document, column.name)
            for column in CatalogDocument.__table__.columns
        }
    )
    other.id, other.slug = str(uuid4()), "earlier-model"
    session.add(other)
    session.flush()
    copy = CatalogDocumentRevision(
        **{
            column.name: getattr(revision, column.name)
            for column in CatalogDocumentRevision.__table__.columns
        }
    )
    copy.id, copy.document_id, copy.slug = str(uuid4()), other.id, other.slug
    if model_content_sha256 is not None:
        document = copying.deepcopy(copy.document)
        models = document["models"]
        assert isinstance(models, list)
        assert isinstance(models[0], dict)
        models[0]["model"] = {
            **models[0]["model"],
            "content_sha256": model_content_sha256,
        }
        copy.document = document
    session.add(copy)
    session.flush()
    return copy


def hold_image_through_another_recipe(lifecycle, sessions, *, now) -> None:
    """Another recipe that builds the same image is installed on the Sparks, so
    its installed ranks keep that image there. No profile of this recipe names
    it and no load of this recipe reuses it."""

    with sessions() as session:
        build = session.scalar(select(RecipeBuild))
        mapping = session.scalar(select(ClusterMapping))
        assert build is not None
        assert mapping is not None
        mapping_id = mapping.id
    install_plan = lifecycle.preview_install(mapping_id, build.id)
    stored_plan = install_plan.stored_plan()
    with sessions.begin() as session:
        other = copy_as_another_recipe(session, build.recipe_revision_id)
        stored = serialize_json_value(stored_plan)
        assert isinstance(stored, dict)
        installation = RecipeInstallation(
            recipe_revision_id=other.id,
            model_content_sha256=_primary_model_sha256(other.document),
            model_artifact_set_sha256=stored_plan.model_artifact_set_sha256,
            mapping_id=mapping_id,
            mapping_generation=install_plan.mapping_generation,
            recipe_build_id=build.id,
            image_digest=build.image_digest,
            plan_digest=install_plan.plan_digest,
            plan={**stored, "recipe_revision_id": other.id},
            state="installed",
            actor="admin",
            created_at=now,
            updated_at=now,
        )
        session.add(installation)
        session.flush()
        for placed in install_plan.nodes:
            session.add(
                InstallationNode(
                    installation_id=installation.id,
                    node_id=placed.node_id,
                    rank=placed.rank,
                    role=placed.role,
                    state="installed",
                    required_bytes=0,
                    installed_bytes=0,
                    updated_at=now,
                )
            )
