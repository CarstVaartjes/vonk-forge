from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy.orm import Session
from vonk_agent_protocol import (
    CompiledExecutionPlan as WireCompiledExecutionPlan,
)
from vonk_agent_protocol import (
    canonical_message,
)
from vonk_control.compiled_execution_plan import (
    EMPTY_SHA256,
    MAX_COMPILED_EXECUTION_PLAN_BYTES,
    CompiledExecutionPlan,
    CompiledExecutionPlanError,
    CompiledModelArtifact,
    DistributionObjectReceipt,
    compile_verified_execution_plan,
    execution_identity_sha256,
    materialized_model_path,
    validate_compiled_launch_payload,
)
from vonk_control.execution_plan_service import (
    ControllerExecutionPlanService,
    ExecutionPlanCompilationError,
    _placement,
    _PlacementTarget,
)
from vonk_control.jobs import _canonical_payload
from vonk_control.models import (
    CatalogDocumentRevision,
    ClusterMappingNode,
    RecipeBuild,
)
from vonk_control.recipe_start_payloads import (
    RecipeStartPlacement,
    _bind_compiled_execution_plan,
)
from vonk_control.runtime_adapters import resolve_runtime_adapter
from vonk_control.runtime_image_preparation import (
    RuntimeImageReceipt as RuntimeImageReceiptWire,
)
from vonk_forge_contracts import (
    RecipeDefinition,
    document_sha256,
    read_recipe,
)
from vonk_forge_contracts.model import ModelFile, ModelReference

from .canonical_recipe_fixtures import canonical_example


def _mapping(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return value


def _sequence(value: object) -> list[object]:
    assert isinstance(value, list)
    return value


def _first_mapping(value: object) -> dict[str, object]:
    return _mapping(_sequence(value)[0])


def _integer(value: object) -> int:
    assert isinstance(value, int)
    return value


def _spec(
    *, recipe_digest: str = "a" * 64, mount_target: str = "/models"
) -> dict[str, object]:
    payload = b"verified model bytes"
    spec: dict[str, object] = {
        "identity": {
            "recipe_revision_sha256": recipe_digest,
            "harness_sha256": "b" * 64,
            "execution_sha256": "0" * 64,
        },
        "model_artifact_set_sha256": "d" * 64,
        "runtime": {
            "interface": "vonk.runtime.v1",
            "adapter": "vllm",
            "adapter_version": 1,
            "telemetry": {
                "engine": "vllm",
                "engine_version": None,
                "metrics_format": "prometheus",
                "metrics_path": "/metrics",
            },
            "image": "localhost/vonk/recipe-build@sha256:" + "1" * 64,
            "architecture": "linux/arm64",
            "entrypoint": ["/opt/vonk/bin/vllm", "serve"],
            "arguments": [],
            "environment": [],
            "writable_paths": [],
        },
        "security": {
            "gpu": True,
            "network_mode": "none",
            "user": "10001:10001",
            "mounts": [{"source": "/run/vonk/models", "target": "/models"}],
        },
        "lifecycle": {"stop_timeout_seconds": 30},
        "topology": {
            "name": "solo",
            "node_count": 1,
            "rank": 0,
            "role": "entrypoint",
        },
        "endpoint": {
            "port": 8000,
            "model_aliases": ["synthetic-tiny"],
            "health_path": "/v1/models",
        },
        "model_dependencies": [
            {
                "selection_id": "primary",
                "publisher": "vonk-forge",
                "slug": "synthetic-tiny-fp16",
                "content_sha256": "e" * 64,
                "artifact_key": "catalog-provenance-only",
            }
        ],
        "artifacts": [
            {
                "id": "weights",
                "selection_id": "primary",
                "file_id": "weights",
                "path": "model.safetensors",
                "sha256": hashlib.sha256(payload).hexdigest(),
                "bytes": len(payload),
                "roles": ["entrypoint", "weights"],
                "mount": {
                    "source": "/run/vonk/models/primary",
                    "target": mount_target,
                },
                "model": {
                    "publisher": "vonk-forge",
                    "slug": "synthetic-tiny-fp16",
                    "content_sha256": "e" * 64,
                },
            }
        ],
    }
    _mapping(spec["identity"])["execution_sha256"] = execution_identity_sha256(spec)
    return spec


def _job_spec() -> dict[str, object]:
    spec = _spec()
    spec["endpoint"] = None
    spec["job"] = {
        "interface": "image-job",
        "input": None,
        "timeout_seconds": 30,
    }
    security = spec["security"]
    assert isinstance(security, dict)
    security["mounts"].append({"source": "/run/vonk/outputs", "target": "/outputs"})
    _mapping(spec["identity"])["execution_sha256"] = execution_identity_sha256(spec)
    return spec


def _model_objects() -> list[dict[str, object]]:
    payload = b"verified model bytes"
    return [
        {
            "model_content_sha256": "e" * 64,
            "file_id": "weights",
            "path": "model.safetensors",
            "sha256": hashlib.sha256(payload).hexdigest(),
            "bytes": len(payload),
            "roles": ["entrypoint", "weights"],
            "distribution_object": {
                "name": "model.safetensors",
                "sha256": hashlib.sha256(payload).hexdigest(),
                "bytes": len(payload),
                "kind": "model",
            },
        }
    ]


def _image(*, build_id: str = "build-1") -> dict[str, object]:
    return {
        "image_digest": "sha256:" + "1" * 64,
        "oci_layout_sha256": "f" * 64,
        "image_bytes": 4096,
        "build_id": build_id,
        "local_image_config_id": "sha256:" + "2" * 64,
        "runtime_interface_label": "v1",
    }


def _compile(
    spec: dict[str, object] | None = None,
    *,
    image: dict[str, object] | None = None,
) -> CompiledExecutionPlan:
    selected_image = _image() if image is None else image
    selected_spec = _spec() if spec is None else spec
    return compile_verified_execution_plan(
        selected_spec,
        model_artifact_set_sha256="d" * 64,
        model_objects=_model_objects(),
        runtime_image=selected_image,
    )


def test_controller_compiler_preserves_canonical_model_path_and_publisher_text() -> (
    None
):
    spec = _spec()
    path = "模型 file_" * 64
    publisher = "发布者 " + "_" * 124
    canonical_file = ModelFile(
        id="weights",
        path=path,
        sha256=hashlib.sha256(b"verified model bytes").hexdigest(),
        size_bytes=len(b"verified model bytes"),
        roles=["entrypoint", "weights"],
    )
    canonical_reference = ModelReference(
        publisher=publisher,
        slug="synthetic-model",
        content_sha256="e" * 64,
    )
    artifact = _sequence(spec["artifacts"])[0]
    assert isinstance(artifact, dict)
    artifact["path"] = canonical_file.path
    artifact["model"]["publisher"] = canonical_reference.publisher
    _mapping(spec["identity"])["execution_sha256"] = execution_identity_sha256(spec)
    model_object = _model_objects()[0]
    model_object["path"] = path
    _mapping(model_object["distribution_object"])["name"] = path

    plan = compile_verified_execution_plan(
        spec,
        model_artifact_set_sha256="d" * 64,
        model_objects=[model_object],
        runtime_image=_image(),
    )
    assert plan.artifacts[0].path == path
    assert plan.artifacts[0].model.publisher == publisher
    assert len(plan.artifacts[0].path) == 512
    assert len(plan.artifacts[0].model.publisher) == 128


def test_prebuilt_plan_binds_exact_file_and_controller_archive_receipts() -> None:
    plan = _compile()

    artifact = plan.artifacts[0]
    assert artifact.sha256 == _model_objects()[0]["sha256"]
    assert artifact.bytes == len(b"verified model bytes")
    assert artifact.mount.source == "/run/vonk/models/primary"
    assert artifact.materialized_path == "/run/vonk/models/primary/model.safetensors"
    assert artifact.roles == ["entrypoint", "weights"]
    assert plan.runtime_image.build_id == "build-1"

    payload = plan.to_compiled_launch_payload(
        _spec(),
        placement={
            "endpoint_address": None,
            "rank": 0,
            "role": "entrypoint",
            "world_size": 1,
            "local_address": None,
            "master_address": None,
            "master_port": None,
            "port": 8000,
            "reserved_memory_bytes": 1,
            "memory_floor_bytes": 0,
        },
    )
    rendered = json.dumps(payload, sort_keys=True)
    assert "repository" not in rendered
    assert '"revision"' not in rendered
    assert "token" not in rendered


def test_compiled_launch_payload_is_the_nested_schema_two_agent_contract() -> None:
    plan = _compile()
    payload = plan.to_compiled_launch_payload(
        _spec(),
        placement={
            "endpoint_address": None,
            "rank": 0,
            "role": "entrypoint",
            "world_size": 1,
            "local_address": None,
            "master_address": None,
            "master_port": None,
            "port": 8000,
            "reserved_memory_bytes": 1,
            "memory_floor_bytes": 0,
        },
    )
    validated = validate_compiled_launch_payload(payload)
    assert set(validated) == {
        "identity",
        "runtime",
        "artifacts",
        "runtime_image",
        "security",
        "topology",
        "lifecycle",
        "endpoint",
        "job",
    }
    wire = WireCompiledExecutionPlan.parse(validated)
    assert wire.runtime.executable == "/opt/vonk/bin/vllm"
    assert wire.runtime.argv == ["serve"]
    assert wire.artifacts[0].selection_id == "primary"
    assert wire.artifacts[0].mount.model_dump() == {"target": "/models"}
    assert wire.security.network_mode == "none"
    assert wire.endpoint is not None
    assert wire.endpoint.port == 8000
    assert wire.job is None


def test_compiled_launch_payload_preserves_missing_endpoint_for_jobs() -> None:
    plan = _compile(_job_spec())
    payload = plan.to_compiled_launch_payload(
        _job_spec(),
        placement={
            "endpoint_address": None,
            "rank": 0,
            "role": "entrypoint",
            "world_size": 1,
            "local_address": None,
            "master_address": None,
            "master_port": None,
            "port": None,
            "reserved_memory_bytes": 1,
            "memory_floor_bytes": 0,
        },
    )

    validated = validate_compiled_launch_payload(payload)
    wire = WireCompiledExecutionPlan.parse(validated)
    assert wire.endpoint is None
    assert wire.job is not None
    assert wire.job.interface == "image-job"
    assert wire.runtime.placement.port is None


def test_compiled_launch_payload_allows_distinct_serving_ports() -> None:
    spec = _spec()
    payload = _compile().to_compiled_launch_payload(
        spec,
        placement={
            "endpoint_address": None,
            "rank": 0,
            "role": "entrypoint",
            "world_size": 1,
            "local_address": None,
            "master_address": None,
            "master_port": None,
            "port": 9000,
            "reserved_memory_bytes": 1,
            "memory_floor_bytes": 0,
        },
    )

    validated = validate_compiled_launch_payload(payload)
    wire = WireCompiledExecutionPlan.parse(validated)
    assert wire.endpoint is not None
    assert wire.endpoint.port == 8000
    assert wire.runtime.placement.port == 9000


@pytest.mark.parametrize("port", [None, 0, 65536, "9000"])
def test_compiled_launch_serving_port_is_required_and_in_range(port: object) -> None:
    with pytest.raises(CompiledExecutionPlanError):
        payload = _compile().to_compiled_launch_payload(
            _spec(),
            placement={
                "endpoint_address": None,
                "rank": 0,
                "role": "entrypoint",
                "world_size": 1,
                "local_address": None,
                "master_address": None,
                "master_port": None,
                "port": port,
                "reserved_memory_bytes": 1,
                "memory_floor_bytes": 0,
            },
        )
        validate_compiled_launch_payload(payload)


def test_compiled_launch_projection_requires_explicit_placement_fields() -> None:
    placement = {
        "endpoint_address": None,
        "rank": 0,
        "role": "entrypoint",
        "world_size": 1,
        "local_address": None,
        "master_address": None,
        "master_port": None,
        "reserved_memory_bytes": 1,
        "memory_floor_bytes": 0,
    }
    with pytest.raises(CompiledExecutionPlanError, match="runtime port is missing"):
        _compile().to_compiled_launch_payload(_spec(), placement=placement)


def test_compiled_launch_projection_validates_before_persisting() -> None:
    spec = _spec()
    endpoint = spec["endpoint"]
    assert isinstance(endpoint, dict)
    endpoint["protocol"] = "legacy"

    with pytest.raises(CompiledExecutionPlanError):
        _compile().to_compiled_launch_payload(
            spec,
            placement={
                "endpoint_address": None,
                "rank": 0,
                "role": "entrypoint",
                "world_size": 1,
                "local_address": None,
                "master_address": None,
                "master_port": None,
                "port": 8000,
                "reserved_memory_bytes": 1,
                "memory_floor_bytes": 0,
            },
        )


def test_compiled_launch_consumer_rejects_malformed_interface_document() -> None:
    payload = _compile().to_compiled_launch_payload(
        _spec(),
        placement={
            "endpoint_address": None,
            "rank": 0,
            "role": "entrypoint",
            "world_size": 1,
            "local_address": None,
            "master_address": None,
            "master_port": None,
            "port": 8000,
            "reserved_memory_bytes": 1,
            "memory_floor_bytes": 0,
        },
    )
    payload["endpoint"] = {"protocol": "openai", "port": 8000}

    with pytest.raises(CompiledExecutionPlanError):
        validate_compiled_launch_payload(payload)


def test_compiled_launch_payload_rejects_document_over_dedicated_ceiling() -> None:
    plan = _compile()
    payload = plan.to_compiled_launch_payload(
        _spec(),
        placement={
            "endpoint_address": None,
            "rank": 0,
            "role": "entrypoint",
            "world_size": 1,
            "local_address": None,
            "master_address": None,
            "master_port": None,
            "port": 8000,
            "reserved_memory_bytes": 1,
            "memory_floor_bytes": 0,
        },
    )
    _mapping(payload["runtime"])["oversized_flat_field"] = (
        "x" * MAX_COMPILED_EXECUTION_PLAN_BYTES
    )
    with pytest.raises(CompiledExecutionPlanError, match="too large"):
        validate_compiled_launch_payload(payload)


def test_controller_produces_real_751_artifact_plan() -> None:
    fixture = json.loads(
        (Path(__file__).parent / "fixtures" / "compiled_plan_751.json").read_text(
            encoding="utf-8"
        )
    )
    plan = validate_compiled_launch_payload(fixture)
    assert len(_sequence(plan["artifacts"])) == 751
    assert len(canonical_message(plan)) > 300 * 1024
    parent_payload, encoded = _canonical_payload(
        {"phases": [{"payload": {"compiled_execution_plan": plan}}]},
        kind="recipe.start",
    )
    assert parent_payload["phases"]
    assert len(encoded) > 300 * 1024


def test_compiled_launch_payload_requires_both_interface_keys_with_one_null() -> None:
    plan = _compile()
    payload = plan.to_compiled_launch_payload(
        _spec(),
        placement={
            "endpoint_address": None,
            "rank": 0,
            "role": "entrypoint",
            "world_size": 1,
            "local_address": None,
            "master_address": None,
            "master_port": None,
            "port": 8000,
            "reserved_memory_bytes": 1,
            "memory_floor_bytes": 0,
        },
    )
    missing = copy.deepcopy(payload)
    del missing["job"]
    with pytest.raises(CompiledExecutionPlanError):
        validate_compiled_launch_payload(missing)

    both = copy.deepcopy(payload)
    both["job"] = {"id": "job-1"}
    with pytest.raises(CompiledExecutionPlanError):
        validate_compiled_launch_payload(both)


def test_compiled_launch_payload_rejects_non_isolated_network_mode() -> None:
    plan = _compile()
    payload = plan.to_compiled_launch_payload(
        _spec(),
        placement={
            "endpoint_address": None,
            "rank": 0,
            "role": "entrypoint",
            "world_size": 1,
            "local_address": None,
            "master_address": None,
            "master_port": None,
            "port": 8000,
            "reserved_memory_bytes": 1,
            "memory_floor_bytes": 0,
        },
    )
    polluted = copy.deepcopy(payload)
    _mapping(polluted["security"])["network_mode"] = "bridge"
    with pytest.raises(CompiledExecutionPlanError):
        validate_compiled_launch_payload(polluted)

    polluted = copy.deepcopy(payload)
    _mapping(polluted["security"])["host_network"] = True
    with pytest.raises(CompiledExecutionPlanError):
        validate_compiled_launch_payload(polluted)


def test_start_claim_binds_live_rank_placement_without_reintroducing_authority() -> (
    None
):
    plan = _compile()
    payload = plan.to_compiled_launch_payload(
        _spec(),
        placement={
            "endpoint_address": None,
            "rank": 0,
            "role": "entrypoint",
            "world_size": 1,
            "local_address": None,
            "master_address": None,
            "master_port": None,
            "port": 8000,
            "reserved_memory_bytes": 1,
            "memory_floor_bytes": 0,
        },
    )
    started = _bind_compiled_execution_plan(
        payload,
        placement=RecipeStartPlacement(
            node_id="spk_" + "a" * 32,
            rank=0,
            role="entrypoint",
            port=8000,
            reserved_memory_bytes=4096,
            memory_floor_bytes=2048,
            memory_kind="unified",
            fabric_address=None,
        ),
        endpoint_address="192.0.2.10",
        master_address=None,
        master_port=None,
        world_size=1,
    )
    placement = WireCompiledExecutionPlan.parse(started).runtime.placement
    assert placement.endpoint_address == "192.0.2.10"
    assert placement.reserved_memory_bytes == 4096
    assert placement.memory_floor_bytes == 2048
    validate_compiled_launch_payload(started)


@pytest.mark.parametrize("rank", [0, 1])
def test_distributed_start_binds_native_fabric_instead_of_bridge_nat(rank: int) -> None:
    payload = _compile().to_compiled_launch_payload(
        _spec(),
        placement={
            "endpoint_address": None,
            "rank": 0,
            "role": "entrypoint",
            "world_size": 1,
            "local_address": None,
            "master_address": None,
            "master_port": None,
            "port": 8000,
            "reserved_memory_bytes": 1,
            "memory_floor_bytes": 0,
        },
    )
    _mapping(payload["topology"]).update(name="dual", node_count=2)
    started = _bind_compiled_execution_plan(
        payload,
        placement=RecipeStartPlacement(
            node_id="spk_" + "a" * 32,
            rank=rank,
            role="entrypoint" if rank == 0 else "worker",
            port=8000,
            reserved_memory_bytes=4096,
            memory_floor_bytes=2048,
            memory_kind="unified",
            fabric_address=f"192.168.100.{10 + rank}",
        ),
        endpoint_address="192.0.2.10" if rank == 0 else None,
        master_address="192.168.100.10",
        master_port=29500,
        world_size=2,
    )
    wire = WireCompiledExecutionPlan.parse(started)
    assert wire.security.network_mode == "host"
    assert wire.runtime.placement.local_address == f"192.168.100.{10 + rank}"


def test_controller_service_binds_canonical_model_cache_and_build_receipts() -> None:

    recipe_document = canonical_example("recipe-source-build.json")
    topology = _mapping(recipe_document["topology"])
    roles = _sequence(topology["roles"])
    entrypoint = _mapping(roles[0])
    resources = _mapping(entrypoint["resources"])
    memory = _mapping(resources["memory"])
    memory["reserve_bytes"] = 32_000_007
    model_document = canonical_example("model-definition.json")
    model_digest = document_sha256(model_document)
    recipe_document["models"][0]["model"]["content_sha256"] = model_digest
    recipe = read_recipe(recipe_document)
    recipe_digest = document_sha256(recipe_document)
    artifact_set_digest = "a" * 64

    class Manifest:
        digest = artifact_set_digest

    class Cache:
        def resolve_artifact_set(self, *, recipe_revision_sha256: str) -> Manifest:
            assert recipe_revision_sha256 == recipe_digest
            return Manifest()

        def manifest_for_artifact_set(self, digest: str) -> Manifest:
            assert digest == artifact_set_digest
            return Manifest()

        def resolve_verified_artifact_set(
            self, digest: str, *, manifest: Manifest | None = None
        ) -> tuple[dict[str, object], ...]:
            assert digest == artifact_set_digest
            # Compilation describes the set in its own requested terms.
            assert isinstance(manifest, Manifest)
            return (
                {
                    "path": "model.safetensors",
                    "sha256": "c" * 64,
                    "bytes": 1024,
                    "file": "controller-owned",
                    "file_id": "weights",
                    "model_content_sha256": model_digest,
                    "roles": ["weights"],
                },
            )

    node = ClusterMappingNode(
        mapping_id="mapping-1",
        node_id="spk_" + "1" * 32,
        rank=0,
        role="entrypoint",
        endpoint_owner=True,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    revision = CatalogDocumentRevision(
        id="revision-1",
        document_id="document-1",
        kind="recipe",
        publisher=recipe.identity.publisher,
        slug=recipe.identity.slug,
        revision_number=1,
        schema_version=2,
        state="active",
        document=recipe_document,
        content_digest=recipe_digest,
        projected={},
        created_by="test",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    build = RecipeBuild(
        id="build-1",
        recipe_revision_id="revision-1",
        builder_node_id="spk_" + "1" * 32,
        source_bundle_sha256="a" * 64,
        build_input_sha256="b" * 64,
        state="succeeded",
        policy_report={},
        plan={},
        image_digest="sha256:" + "1" * 64,
        oci_layout_sha256="f" * 64,
        image_bytes=4096,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
    )

    def runtime_receipt(
        _document: Mapping[str, object],
        image_digest: str,
        _runtime_spec: Mapping[str, object],
    ) -> RuntimeImageReceiptWire:
        adapter = resolve_runtime_adapter(recipe.runtime.engine, recipe.topology)
        return RuntimeImageReceiptWire(
            schema_version=2,
            distribution_publisher=recipe.identity.publisher,
            distribution_slug=recipe.identity.slug,
            distribution_content_sha256=recipe_digest,
            image_digest=image_digest,
            oci_archive_sha256="f" * 64,
            image_bytes=4096,
            local_image_config_id="sha256:" + "2" * 64,
            architecture="linux-arm64",
            runtime_interface="vonk.runtime.v1",
            archive_path="/run/vonk/image-cache/" + "f" * 64,
            recorded_at="2026-01-01T00:00:00+00:00",
            build_id=build.id,
            runtime_interface_label="v1",
            runtime_adapter=adapter.adapter_id,
            runtime_adapter_sha256=adapter.digest,
        )

    service = ControllerExecutionPlanService(
        Cache(), runtime_image_resolver=runtime_receipt
    )
    plans = service.compile_installation(
        Session(),
        revision=revision,
        build=build,
        mapping_nodes=(node,),
        parameters={},
        resolved_entities={
            "models": (
                SimpleNamespace(document=model_document, content_digest=model_digest),
            )
        },
    )

    payload = plans[node.node_id]
    validate_compiled_launch_payload(payload)
    payload_wire = WireCompiledExecutionPlan.parse(payload)
    assert payload_wire.identity.model_artifact_set_sha256 == artifact_set_digest
    assert payload_wire.runtime.placement.memory_floor_bytes == 32_000_007
    assert payload_wire.artifacts[0].path == "model.safetensors"
    assert payload_wire.runtime_image.build_id == "build-1"
    assert "repository" not in json.dumps(payload, sort_keys=True)


def test_controller_service_rejects_invalid_recipe_topology_at_canonical_boundary() -> (
    None
):
    document = canonical_example("recipe-source-build.json")
    document.pop("topology")

    class Cache:
        def resolve_artifact_set(self, **_kwargs: object) -> object:
            raise AssertionError("invalid recipes must fail before cache resolution")

    revision = CatalogDocumentRevision(
        id="revision-1",
        document_id="document-1",
        kind="recipe",
        publisher="publisher",
        slug="recipe",
        revision_number=1,
        schema_version=2,
        state="active",
        document=document,
        content_digest="a" * 64,
        projected={},
        created_by="test",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    service = ControllerExecutionPlanService(Cache())
    with pytest.raises(
        ExecutionPlanCompilationError,
        match="recipe does not satisfy the canonical contract",
    ):
        service.compile_installation(
            Session(),
            revision=revision,
            build=None,
            mapping_nodes=(),
            parameters={},
        )


def test_placement_rejects_unresolved_role_and_endpoint() -> None:
    recipe = RecipeDefinition.model_validate(
        canonical_example("recipe-source-build.json")
    )
    with pytest.raises(ExecutionPlanCompilationError, match="mapped role"):
        _placement(
            recipe,
            {"endpoint": {"port": 8000}},
            _PlacementTarget(rank=0, role="missing"),
            1,
        )

    with pytest.raises(ExecutionPlanCompilationError, match="endpoint"):
        _placement(recipe, {}, _PlacementTarget(rank=0, role="entrypoint"), 1)


def test_generated_schema_two_fixture_preserves_scoped_collisions_empty_file_and_isolation() -> (
    None
):
    fixture = json.loads(
        (Path(__file__).parent / "fixtures" / "compiled_workload_v2.json").read_text(
            encoding="utf-8"
        )
    )
    validated = validate_compiled_launch_payload(fixture)
    wire = WireCompiledExecutionPlan.parse(validated)
    artifacts = wire.artifacts
    assert [item.path for item in artifacts].count("config.json") == 2
    assert {
        (item.selection_id, item.file_id, item.sha256, item.size_bytes)
        for item in artifacts
    } >= {
        (
            "primary",
            "config-66402a06352a",
            """66402a06352ac861bc9012a26678e6d5e11a5fd22180165fc19c8a27d3a9e079""",
            72897,
        ),
        (
            "dependency-qwen3-8-27b-dspark-b3c99101",
            "config-dd65fb1b01c2",
            "dd65fb1b01c2adea69512ff2990a79d58eb7fe2c7ea97375aa66f657a29a5bfd",
            2448,
        ),
    }
    empty = next(item for item in artifacts if item.size_bytes == 0)
    assert empty.sha256 == EMPTY_SHA256
    assert empty.roles == ["entrypoint"]
    assert empty.path == "__init__.py"
    assert wire.security.network_mode == "none"
    runtime_image = wire.runtime_image
    assert runtime_image.local_image_config_id != runtime_image.image_digest
    assert runtime_image.local_image_reference == (
        "localhost/vonk/compiled-runtime-"
        f"{runtime_image.oci_layout_sha256}@{runtime_image.image_digest}"
    )
    assert runtime_image.runtime_interface_label == "v1"
    argv = wire.runtime.argv
    assert "--served-model-name" in argv
    assert argv[argv.index("--served-model-name") + 1] == "qwen3-8-27b-collision"
    assert any(
        item.name == "XDG_CACHE_HOME" and item.value == "/outputs/cache"
        for item in wire.runtime.env
    )
    assert any(
        item.name == "TMPDIR" and item.value == "/outputs/tmp"
        for item in wire.runtime.env
    )
    assert {mount.source for mount in wire.security.mounts} >= {
        "model",
        "outputs",
    }


def test_upstream_authority_cannot_enter_compiled_receipts() -> None:
    polluted = _spec()
    model = _first_mapping(polluted["artifacts"])["model"]
    assert isinstance(model, dict)
    model["repository"] = "huggingface.co/private/model"

    with pytest.raises(CompiledExecutionPlanError, match="upstream authority"):
        _compile(polluted)


def test_plan_rejects_incomplete_selected_cache_receipt() -> None:
    objects = _model_objects()
    objects[0]["path"] = "config.json"
    _mapping(objects[0]["distribution_object"])["name"] = "config.json"
    with pytest.raises(CompiledExecutionPlanError, match="path, digest"):
        compile_verified_execution_plan(
            _spec(),
            model_artifact_set_sha256="d" * 64,
            model_objects=objects,
            runtime_image=_image(),
        )


def test_plan_rejects_missing_selected_cache_bytes() -> None:
    with pytest.raises(CompiledExecutionPlanError):
        compile_verified_execution_plan(
            _spec(),
            model_artifact_set_sha256="d" * 64,
            model_objects=[],
            runtime_image=_image(),
        )


def test_cache_authority_digest_is_explicit_when_runtime_spec_omits_it() -> None:
    spec = _spec()
    spec.pop("model_artifact_set_sha256")

    plan = compile_verified_execution_plan(
        spec,
        model_artifact_set_sha256="d" * 64,
        model_objects=_model_objects(),
        runtime_image=_image(),
    )
    assert plan.model_artifact_set_sha256 == "d" * 64


def test_runtime_spec_cannot_disagree_with_cache_authority_digest() -> None:
    with pytest.raises(CompiledExecutionPlanError, match="does not match"):
        compile_verified_execution_plan(
            _spec(),
            model_artifact_set_sha256="1" * 64,
            model_objects=_model_objects(),
            runtime_image=_image(),
        )


def test_declared_execution_identity_must_cover_compiled_launch_facts() -> None:
    spec = _spec()
    _mapping(spec["identity"])["execution_sha256"] = "0" * 64
    with pytest.raises(CompiledExecutionPlanError, match="launch facts"):
        compile_verified_execution_plan(
            spec,
            model_artifact_set_sha256="d" * 64,
            model_objects=_model_objects(),
            runtime_image=_image(),
        )


def test_plan_identity_does_not_mutate_canonical_runtime_input() -> None:
    spec = _spec()
    original = copy.deepcopy(spec)
    _compile(spec)
    assert spec == original


def _collision_spec() -> dict[str, object]:
    spec = _spec()
    spec["model_artifact_set_sha256"] = "9" * 64
    spec["model_dependencies"] = [
        {
            "selection_id": "primary",
            "publisher": "radixark",
            "slug": "qwen3-8-27b-nvfp4-009632fe",
            "content_sha256": "29b9d51b0a6dde0c2acae929c6d2a5651d19fb8a7572915f4c096e3b5bc5329b",
        },
        {
            "selection_id": "draft",
            "publisher": "radixark",
            "slug": "qwen3-8-27b-dspark-b3c99101",
            "content_sha256": "4091ffe98645f39f163c52efe1228f5385970df1d631df050eea1628b6721888",
        },
    ]
    qwen_sha = "66402a06352ac861bc9012a26678e6d5e11a5fd22180165fc19c8a27d3a9e079"
    dspark_sha = "dd65fb1b01c2adea69512ff2990a79d58eb7fe2c7ea97375aa66f657a29a5bfd"
    spec["artifacts"] = [
        {
            "id": "primary-config-66402a06352a",
            "selection_id": "primary",
            "file_id": "config-66402a06352a",
            "path": "config.json",
            "sha256": qwen_sha,
            "bytes": 72897,
            "roles": ["entrypoint"],
            "mount": {
                "source": "/run/vonk/models/primary",
                "target": "/models/target",
            },
            "model": {
                "publisher": "radixark",
                "slug": "qwen3-8-27b-nvfp4-009632fe",
                "content_sha256": "29b9d51b0a6dde0c2acae929c6d2a5651d19fb8a7572915f4c096e3b5bc5329b",
            },
        },
        {
            "id": "dependency-qwen3-8-27b-dspark-b3c99101-config-dd65fb1b01c2",
            "selection_id": "draft",
            "file_id": "config-dd65fb1b01c2",
            "path": "config.json",
            "sha256": dspark_sha,
            "bytes": 2448,
            "roles": ["entrypoint"],
            "mount": {
                "source": "/run/vonk/models/draft",
                "target": "/models/draft",
            },
            "model": {
                "publisher": "radixark",
                "slug": "qwen3-8-27b-dspark-b3c99101",
                "content_sha256": "4091ffe98645f39f163c52efe1228f5385970df1d631df050eea1628b6721888",
            },
        },
    ]
    _mapping(spec["identity"])["execution_sha256"] = execution_identity_sha256(spec)
    return spec


def _collision_objects() -> list[dict[str, object]]:
    return [
        {
            "model_content_sha256": "29b9d51b0a6dde0c2acae929c6d2a5651d19fb8a7572915f4c096e3b5bc5329b",
            "file_id": "config-66402a06352a",
            "path": "config.json",
            "sha256": "66402a06352ac861bc9012a26678e6d5e11a5fd22180165fc19c8a27d3a9e079",
            "bytes": 72897,
            "roles": ["entrypoint"],
            "distribution_object": {
                "name": "config.json",
                "sha256": "66402a06352ac861bc9012a26678e6d5e11a5fd22180165fc19c8a27d3a9e079",
                "bytes": 72897,
                "kind": "model",
            },
        },
        {
            "model_content_sha256": "4091ffe98645f39f163c52efe1228f5385970df1d631df050eea1628b6721888",
            "file_id": "config-dd65fb1b01c2",
            "path": "config.json",
            "sha256": "dd65fb1b01c2adea69512ff2990a79d58eb7fe2c7ea97375aa66f657a29a5bfd",
            "bytes": 2448,
            "roles": ["entrypoint"],
            "distribution_object": {
                "name": "config.json",
                "sha256": "dd65fb1b01c2adea69512ff2990a79d58eb7fe2c7ea97375aa66f657a29a5bfd",
                "bytes": 2448,
                "kind": "model",
            },
        },
    ]


def test_plan_rejects_two_files_materializing_to_one_selection_path() -> None:
    document = _compile().model_dump(mode="json")
    duplicate = copy.deepcopy(document["artifacts"][0])
    duplicate["id"] = "duplicate"
    duplicate["file_id"] = "duplicate"
    document["artifacts"].append(duplicate)
    with pytest.raises(ValidationError, match="physical identity"):
        CompiledExecutionPlan.model_validate(document)


def test_plan_rejects_duplicate_final_projection_target() -> None:
    document = _compile().model_dump(mode="json")
    duplicate = copy.deepcopy(document["artifacts"][0])
    duplicate["id"] = "duplicate-projection"
    document["artifacts"].append(duplicate)
    with pytest.raises(ValidationError, match="mount target"):
        CompiledExecutionPlan.model_validate(document)


def test_plan_preserves_duplicate_physical_artifact_as_two_projections() -> None:
    document = _compile().model_dump(mode="json")
    duplicate = copy.deepcopy(document["artifacts"][0])
    duplicate["id"] = "second-projection"
    duplicate["mount"]["target"] = "/models/target"
    document["artifacts"].append(duplicate)
    plan = CompiledExecutionPlan.model_validate(document)
    assert [(artifact.mount.target, artifact.path) for artifact in plan.artifacts] == [
        ("/models", "model.safetensors"),
        ("/models/target", "model.safetensors"),
    ]


def test_qwen_config_collision_binds_model_identity_and_preserves_file_path(
    tmp_path,
) -> None:
    plan = compile_verified_execution_plan(
        _collision_spec(),
        model_artifact_set_sha256="9" * 64,
        model_objects=_collision_objects(),
        runtime_image=_image(),
    )
    models_root = tmp_path / "run" / "vonk" / "models"
    sizes = {"primary": 72897, "draft": 2448}
    prefixes = {"primary": b"qwen config", "draft": b"dspark config"}
    payloads = {
        selection: (prefix * ((size // len(prefix)) + 1))[:size]
        for selection, size in sizes.items()
        for prefix in [prefixes[selection]]
    }
    paths = {}
    for artifact in plan.artifacts:
        path = materialized_model_path(models_root, artifact)
        path.parent.mkdir(parents=True)
        path.write_bytes(payloads[artifact.selection_id])
        paths[artifact.selection_id] = path

    assert paths["primary"].name == "config.json"
    assert paths["draft"].name == "config.json"
    assert paths["primary"] != paths["draft"]
    assert paths["primary"].stat().st_size == 72897
    assert paths["draft"].stat().st_size == 2448
    assert paths["primary"].read_bytes().startswith(b"qwen config")
    assert paths["draft"].read_bytes().startswith(b"dspark config")
    assert all(
        item.sha256 not in str(path)
        for path in paths.values()
        for item in plan.artifacts
    )
    assert {item.mount.source for item in plan.artifacts} == {
        "/run/vonk/models/primary",
        "/run/vonk/models/draft",
    }


def test_qwen_collision_rejects_wrong_model_object_even_when_path_matches() -> None:
    with pytest.raises(CompiledExecutionPlanError, match="not covered"):
        compile_verified_execution_plan(
            _collision_spec(),
            model_artifact_set_sha256="9" * 64,
            model_objects=_collision_objects()[1:],
            runtime_image=_image(),
        )


def test_empty_model_support_file_requires_empty_digest_and_keeps_original_path(
    tmp_path,
) -> None:
    spec = _spec()
    _first_mapping(spec["artifacts"]).update(
        {
            "id": "tokenizer-config",
            "file_id": "tokenizer-config",
            "path": "tokenizer_config.json",
            "sha256": EMPTY_SHA256,
            "bytes": 0,
            "roles": ["auxiliary"],
        }
    )
    _mapping(spec["identity"])["execution_sha256"] = execution_identity_sha256(spec)
    objects = [
        {
            "model_content_sha256": "e" * 64,
            "file_id": "tokenizer-config",
            "path": "tokenizer_config.json",
            "sha256": EMPTY_SHA256,
            "bytes": 0,
            "roles": ["auxiliary"],
            "distribution_object": {
                "name": "tokenizer_config.json",
                "sha256": EMPTY_SHA256,
                "bytes": 0,
                "kind": "model",
            },
        }
    ]
    plan = compile_verified_execution_plan(
        spec,
        model_artifact_set_sha256="d" * 64,
        model_objects=objects,
        runtime_image=_image(),
    )
    assert plan.model_artifact_set_bytes == 0
    artifact = plan.artifacts[0]
    path = materialized_model_path(tmp_path / "models", artifact)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"")
    assert path.name == "tokenizer_config.json"
    assert path.read_bytes() == b""

    _first_mapping(spec["artifacts"])["sha256"] = "a" * 64
    _mapping(spec["identity"])["execution_sha256"] = execution_identity_sha256(spec)
    with pytest.raises(CompiledExecutionPlanError, match="digest or size"):
        compile_verified_execution_plan(
            spec,
            model_artifact_set_sha256="d" * 64,
            model_objects=objects,
            runtime_image=_image(),
        )
    with pytest.raises(ValidationError, match="only an empty model"):
        DistributionObjectReceipt.model_validate(
            {
                "name": "tokenizer_config.json",
                "sha256": "a" * 64,
                "bytes": 0,
                "kind": "model",
            }
        )
    invalid_roles = plan.artifacts[0].model_dump(mode="json")
    invalid_roles["roles"] = ["weights"]
    with pytest.raises(ValidationError, match="non-weight support"):
        CompiledModelArtifact.model_validate(invalid_roles)


def test_execution_identity_covers_compiled_launch_facts_and_ignores_notes() -> None:
    base = _spec()
    baseline = execution_identity_sha256(base)
    notes = copy.deepcopy(base)
    notes["editorial_notes"] = {"release": "same bytes"}
    _first_mapping(notes["model_dependencies"])["artifact_key"] = (
        "new-provenance-handle"
    )
    assert execution_identity_sha256(notes) == baseline

    changes = []
    for key, value in (
        ("runtime", {"arguments": [{"name": "--max-model-len", "value": 4096}]}),
        ("security", {"user": "10002:10002"}),
        ("lifecycle", {"stop_timeout_seconds": 45}),
        ("topology", {"rank": 1}),
        ("endpoint", {"port": 8001}),
    ):
        changed = copy.deepcopy(base)
        _mapping(changed[key]).update(_mapping(value))
        changes.append(execution_identity_sha256(changed))
    changed_artifact = copy.deepcopy(base)
    _mapping(_first_mapping(changed_artifact["artifacts"])["mount"])["target"] = (
        "/models/changed"
    )
    changes.append(execution_identity_sha256(changed_artifact))

    assert all(value != baseline for value in changes)
    assert len(set(changes)) == len(changes)
