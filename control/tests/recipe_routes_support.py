"""Shared helpers for the recipe_routes tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from importlib.resources import files
from pathlib import Path
from uuid import uuid4

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol.claims import AgentRuntimeIdentity
from vonk_control.gateway.route_runtime import (
    AtomicRouteBundlePublisher,
)
from vonk_control.model_primitives import Base
from vonk_control.models.cache import RecipeBuild
from vonk_control.models.catalog import CatalogDocument, CatalogDocumentRevision
from vonk_control.models.execution import (
    InstallationNode,
    RecipeInstallation,
    RecipeRun,
    RunNode,
)
from vonk_control.models.fleet import AgentCertificate, AgentNode
from vonk_control.models.mapping import ClusterMapping, ClusterMappingNode
from vonk_control.models.routes import (
    RoutePublication,
    RoutePublicationOwner,
)
from vonk_control.presence import ManagementAddressPolicy
from vonk_control.recipe_routes.publisher import AtomicRecipeRoutePublisher
from vonk_control.recipe_routes.service import RecipeRouteService
from vonk_forge_contracts import (
    ModelDefinition,
    RecipeDefinition,
    document_sha256,
)

from .display_fixtures import reword
from .node_contacts import contact_row
from .runtime_identity_support import PACKAGED_RUNTIME_IDENTITY, inventory_items_fixture
from .stored_documents_support import valid_policy_report

NOW = datetime(2026, 8, 7, 12, tzinfo=UTC)


GATEWAY = "https://control.test.example/v1"


def _recipe_run(session: Session, run_id: str) -> RecipeRun:
    """Return the run row the test itself wrote, or fail on a missing row."""

    run = session.get(RecipeRun, run_id)
    assert run is not None
    return run


def _publication_owner(session: Session) -> RoutePublicationOwner:
    """Return the singleton route publication owner, which must exist here."""

    owner = session.get(RoutePublicationOwner, 1)
    assert owner is not None
    return owner


def _publication(session: Session, authority_id: str | None) -> RoutePublication:
    """Return the publication for an owner's authority, which must exist here."""

    publication = session.get(RoutePublication, authority_id)
    assert publication is not None
    return publication


class MutableClock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def setup(
    tmp_path: Path,
    *,
    ranks=2,
    stale=False,
    failed_rank=False,
    validate=lambda _: True,
    clock=None,
    run_alias="qwen",
    runtime_model_aliases=("qwen",),
    interfaces=None,
    settings=None,
    endpoint_owner_rank=0,
    exact_distributed=True,
    engine=None,
):
    tmp_path.mkdir(parents=True, exist_ok=True)
    if engine is None:
        engine = create_engine(f"sqlite:///{tmp_path / 'routes.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    nodes = tuple("spk_" + f"{index + 1:032x}" for index in range(ranks))
    if not 0 <= endpoint_owner_rank < ranks:
        raise ValueError("endpoint owner rank is outside the fixture")
    with sessions.begin() as session:
        session.add_all(
            AgentNode(
                node_id=node,
                state="active",
                inventory_snapshot=inventory_items_fixture(
                    node,
                    observed_at=None,
                    identity=AgentRuntimeIdentity.model_validate(
                        {**PACKAGED_RUNTIME_IDENTITY, "architecture": "linux-arm64"}
                    ),
                ),
            )
            for node in nodes
        )
        model_document = json.loads(
            files("vonk_forge_contracts")
            .joinpath("examples", "model-definition.json")
            .read_text(encoding="utf-8")
        )
        model = ModelDefinition.model_validate(model_document)
        recipe_document = json.loads(
            files("vonk_forge_contracts")
            .joinpath("examples", "recipe-source-build.json")
            .read_text(encoding="utf-8")
        )
        recipe_document["identity"]["slug"] = "qwen"
        reword(recipe_document, "Qwen")
        recipe_document["models"][0]["model"]["content_sha256"] = document_sha256(
            model_document
        )
        if interfaces is None:
            recipe_document["interfaces"] = [
                {
                    "adapter": "openai",
                    "port": 8000,
                    "model_aliases": list(runtime_model_aliases) or ["invalid alias"],
                    "health_path": "/v1/models",
                }
            ]
        elif interfaces == [{"adapter": "video-job"}]:
            recipe_document["interfaces"] = [
                {
                    "adapter": "video-job",
                    "output": {
                        "max_total_bytes": 1,
                        "slots": [
                            {
                                "id": "output",
                                "label": "Output",
                                "description": "Rendered output",
                                "media_types": ["video/mp4"],
                                "extensions": [".mp4"],
                                "min_files": 1,
                                "max_files": 1,
                                "max_file_bytes": 1,
                                "max_total_bytes": 1,
                            }
                        ],
                    },
                }
            ]
            recipe_document["settings"] = {"kind": "job", "knobs": {}}
            recipe_document["validation"]["serving"] = {
                "interface": "video-job",
                "checks": [
                    {
                        "name": "output",
                        "kind": "video-job.output",
                        "request": {
                            "transport": "job",
                            "fixture": "fixture",
                            "output_slot": "output",
                        },
                        "assertions": ["artifact.output"],
                    }
                ],
            }
        else:
            recipe_document["interfaces"] = interfaces
        if settings is not None:
            recipe_document["settings"] = settings
        recipe = RecipeDefinition.model_validate(recipe_document)
        recipe_id = str(uuid4())
        revision_id = str(uuid4())
        model_id = str(uuid4())
        model_revision_id = str(uuid4())
        session.add_all(
            [
                CatalogDocument(
                    id=recipe_id,
                    kind="recipe",
                    publisher=recipe.identity.publisher,
                    slug=recipe.identity.slug,
                    title=recipe.identity.slug,
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
            ]
        )
        session.flush()
        revision = CatalogDocumentRevision(
            id=revision_id,
            document_id=recipe_id,
            kind="recipe",
            publisher=recipe.identity.publisher,
            slug=recipe.identity.slug,
            revision_number=1,
            schema_version=2,
            state="active",
            document=recipe_document,
            content_digest=document_sha256(recipe_document),
            projected={},
            created_by="admin",
            created_at=NOW,
        )
        session.add_all(
            [
                revision,
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
                    content_digest=document_sha256(model_document),
                    projected={},
                    created_by="admin",
                    created_at=NOW,
                ),
            ]
        )
        session.flush()
        mapping = ClusterMapping(
            recipe_revision_id=revision.id,
            topology_name=f"{ranks}-node",
            generation=1,
            node_count=ranks,
            state="ready",
            parameters={},
            placement_digest="d" * 64,
            endpoint_owner_node_id=nodes[endpoint_owner_rank],
            created_by="admin",
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(mapping)
        session.flush()
        session.add_all(
            ClusterMappingNode(
                mapping_id=mapping.id,
                node_id=node,
                rank=rank,
                role="entrypoint" if rank == endpoint_owner_rank else "worker",
                endpoint_owner=rank == endpoint_owner_rank,
                created_at=NOW,
            )
            for rank, node in enumerate(nodes)
        )
        build = RecipeBuild(
            recipe_revision_id=revision.id,
            builder_node_id=nodes[0],
            source_bundle_sha256="e" * 64,
            build_input_sha256="f" * 64,
            state="succeeded",
            policy_report=valid_policy_report(),
            plan={},
            image_digest="sha256:" + "9" * 64,
            oci_layout_sha256="8" * 64,
            image_bytes=1,
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(build)
        session.flush()
        installation = RecipeInstallation(
            recipe_revision_id=revision.id,
            mapping_id=mapping.id,
            mapping_generation=1,
            recipe_build_id=build.id,
            image_digest=build.image_digest,
            plan_digest="b" * 64,
            plan={},
            state="installed",
            actor="admin",
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(installation)
        session.flush()
        session.add_all(
            InstallationNode(
                installation_id=installation.id,
                node_id=node,
                rank=rank,
                role="entrypoint" if rank == endpoint_owner_rank else "worker",
                state="installed",
                required_bytes=1,
                installed_bytes=1,
                updated_at=NOW,
            )
            for rank, node in enumerate(nodes)
        )
        run = RecipeRun(
            installation_id=installation.id,
            mapping_id=mapping.id,
            mapping_generation=1,
            alias=run_alias,
            plan_digest="c" * 64,
            plan={
                "schema_version": 1,
                "observation_schema_version": 2,
                "run_generation": 1,
                "installation_id": installation.id,
                "mapping_id": mapping.id,
                "mapping_generation": 1,
                "recipe_revision_id": revision.id,
                "plan_digest": "c" * 64,
                "nodes": [
                    {
                        "node_id": node,
                        "rank": rank,
                        "role": "entrypoint"
                        if rank == endpoint_owner_rank
                        else "worker",
                        "endpoint_owner": rank == endpoint_owner_rank,
                        "port": 8000,
                        "allowed": True,
                        "inventory_observed_at": NOW.isoformat(),
                        "memory_kind": "unified",
                        "memory_pool": "shared",
                        "required_memory_bytes": 100,
                        "available_memory_bytes": None,
                        "active_reserved_bytes": 0,
                        "free_after_bytes": None,
                        "memory_floor_bytes": 0,
                        "fabric_address": None,
                        "fabric_bandwidth_mbps": None,
                        "rendezvous_port": None,
                        "blockers": [],
                        "warnings": [],
                    }
                    for rank, node in enumerate(nodes)
                ],
            },
            state="running",
            actor="admin",
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(run)
        session.flush()
        for rank, node in enumerate(nodes):
            session.add(
                RunNode(
                    run_id=run.id,
                    node_id=node,
                    rank=rank,
                    role="entrypoint" if rank == endpoint_owner_rank else "worker",
                    state="failed" if failed_rank and rank == ranks - 1 else "running",
                    port=8000,
                    reserved_memory_bytes=100,
                    endpoint={"url": f"http://10.0.0.{rank + 2}:8000"},
                    observed_run_generation=(
                        run.run_generation if exact_distributed else None
                    ),
                    observation_observed_at=NOW if exact_distributed else None,
                    observation_endpoint_ready=(
                        True
                        if exact_distributed and rank == endpoint_owner_rank
                        else None
                    ),
                    updated_at=NOW - (timedelta(seconds=301) if stale else timedelta()),
                )
            )
    applied: list[bytes] = []

    def apply(content: bytes) -> bool:
        accepted = validate(content)
        if accepted is True:
            applied.append(content)
        return accepted

    clock = clock or (lambda: NOW)
    publisher = AtomicRouteBundlePublisher(tmp_path / "litellm", validate_litellm=apply)
    service = RecipeRouteService(
        sessions,
        publisher=AtomicRecipeRoutePublisher(publisher),
        management_policy=ManagementAddressPolicy.parse("10.0.0.0/24"),
        clock=clock,
        maximum_age_seconds=120,
    )
    return service, publisher, applied, run.id


def add_running_run(
    service: RecipeRouteService,
    source_run_id: str,
    *,
    alias: str,
    route_state: str,
    identity: int,
) -> str:
    node_id = "spk_" + f"{identity:032x}"
    with service.sessions.begin() as session:
        source = session.get(RecipeRun, source_run_id)
        assert source is not None
        source_installation = session.get(RecipeInstallation, source.installation_id)
        assert source_installation is not None
        session.add(
            AgentNode(
                node_id=node_id,
                state="active",
                inventory_snapshot=inventory_items_fixture(
                    node_id,
                    observed_at=None,
                    identity=AgentRuntimeIdentity.model_validate(
                        {**PACKAGED_RUNTIME_IDENTITY, "architecture": "linux-arm64"}
                    ),
                ),
            )
        )
        mapping = ClusterMapping(
            recipe_revision_id=source_installation.recipe_revision_id,
            topology_name="single-node",
            generation=1,
            node_count=1,
            state="ready",
            parameters={},
            placement_digest=f"{identity:x}" * 64,
            endpoint_owner_node_id=node_id,
            created_by="admin",
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(mapping)
        session.flush()
        session.add(
            ClusterMappingNode(
                mapping_id=mapping.id,
                node_id=node_id,
                rank=0,
                role="entrypoint",
                endpoint_owner=True,
                created_at=NOW,
            )
        )
        installation = RecipeInstallation(
            recipe_revision_id=source_installation.recipe_revision_id,
            mapping_id=mapping.id,
            mapping_generation=1,
            recipe_build_id=source_installation.recipe_build_id,
            image_digest=source_installation.image_digest,
            plan_digest=f"{identity + 4:x}" * 64,
            plan={},
            state="installed",
            actor="admin",
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(installation)
        session.flush()
        session.add(
            InstallationNode(
                installation_id=installation.id,
                node_id=node_id,
                rank=0,
                role="entrypoint",
                state="installed",
                required_bytes=1,
                installed_bytes=1,
                updated_at=NOW,
            )
        )
        run = RecipeRun(
            installation_id=installation.id,
            mapping_id=mapping.id,
            mapping_generation=mapping.generation,
            alias=alias,
            plan_digest=f"{identity:x}" * 64,
            plan={
                "schema_version": 1,
                "observation_schema_version": 2,
                "run_generation": 1,
                "installation_id": installation.id,
                "mapping_id": mapping.id,
                "mapping_generation": mapping.generation,
                "recipe_revision_id": source_installation.recipe_revision_id,
                "plan_digest": f"{identity:x}" * 64,
                "nodes": [
                    {
                        "node_id": node_id,
                        "rank": 0,
                        "role": "entrypoint",
                        "endpoint_owner": True,
                        "port": 8000,
                        "allowed": True,
                        "inventory_observed_at": NOW.isoformat(),
                        "memory_kind": "unified",
                        "memory_pool": "shared",
                        "required_memory_bytes": 100,
                        "available_memory_bytes": None,
                        "active_reserved_bytes": 0,
                        "free_after_bytes": None,
                        "memory_floor_bytes": 0,
                        "fabric_address": None,
                        "fabric_bandwidth_mbps": None,
                        "rendezvous_port": None,
                        "blockers": [],
                        "warnings": [],
                    }
                ],
            },
            state="running",
            route_state=route_state,
            actor="admin",
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(run)
        session.flush()
        session.add(
            RunNode(
                run_id=run.id,
                node_id=node_id,
                rank=0,
                role="entrypoint",
                state="running",
                port=8000,
                reserved_memory_bytes=100,
                endpoint={"url": f"http://10.0.0.{identity}:8000"},
                observed_run_generation=1,
                observation_observed_at=NOW,
                observation_endpoint_ready=True,
                updated_at=NOW,
            )
        )
        return run.id


def atomic_service(
    service: RecipeRouteService, root: Path, clock
) -> RecipeRouteService:
    runtime = AtomicRouteBundlePublisher(root)
    return RecipeRouteService(
        service.sessions,
        publisher=AtomicRecipeRoutePublisher(runtime),
        management_policy=ManagementAddressPolicy.parse("10.0.0.0/24"),
        clock=clock,
        maximum_age_seconds=120,
    )


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _agents_reached_the_controller(service, at: datetime) -> None:
    with service.sessions.begin() as session:
        for node in session.query(RunNode):
            presence = session.get(AgentNode, node.node_id)
            if contact_row(presence).management_address is None:
                serial = f"serial-{node.node_id[-4:]}"
                if session.get(AgentCertificate, serial) is None:
                    session.add(
                        AgentCertificate(
                            serial=serial,
                            node_id=node.node_id,
                            not_before=at - timedelta(days=1),
                            not_after=at + timedelta(days=1),
                            fingerprint=f"fingerprint-{serial}",
                        )
                    )
                    session.flush()
                contact_row(presence).contact_certificate_serial = serial
                contact_row(presence).management_address = "192.168.1.10"
            contact_row(presence).management_observed_at = at


def _recipe_owner_id(session) -> str:
    owner = session.get(RoutePublicationOwner, 1)
    assert owner is not None and owner.authority_id is not None
    return owner.authority_id


def _live_models(root: Path) -> list[str]:
    marker = json.loads((root / "activation.json").read_text())
    config = json.loads(
        (root / "generations" / marker["directory"] / "litellm.json").read_text()
    )
    return [entry["model_name"] for entry in config["model_list"]]
