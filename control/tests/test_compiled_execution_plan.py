from __future__ import annotations

import copy
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from vonk_control.compiled_execution_plan import (
    EMPTY_SHA256,
    CompiledExecutionPlan,
    CompiledExecutionPlanError,
    CompiledModelArtifact,
    DistributionObjectReceipt,
    compile_verified_execution_plan,
    execution_identity_sha256,
    materialized_model_path,
    validate_compiled_launch_payload,
)
from vonk_control.agent_api import AgentApiServices
from vonk_control.agent_jobs import AgentJobService
from vonk_control.api import create_app
from vonk_control.audit import MemoryAuditStore
from vonk_control.auth import TokenCodec
from vonk_control.execution_plan_service import ControllerExecutionPlanService
from vonk_control.models import (
    AgentCertificate,
    AgentNode,
    Base,
    InstallationNode,
    RecipeInstallation,
)
from vonk_control.presence import AgentPresenceService, ManagementAddressPolicy
from vonk_control.recipe_operations import _compiled_plan_for_start
from vonk_control.source_bundles import SourceBundleStore


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
            "image": "registry.example/vonk/vllm@sha256:" + "1" * 64,
            "architecture": "linux/arm64",
            "entrypoint": ["/opt/vonk/bin/vllm", "serve"],
            "arguments": [],
            "environment": [],
            "writable_paths": [],
        },
        "security": {
            "devices": [],
            "capabilities": [],
            "host_network": False,
            "privileged": False,
            "user": "10001:10001",
            "mounts": [
                {
                    "source": "/run/vonk/models",
                    "target": "/models",
                    "read_only": True,
                }
            ],
            "read_only_root": True,
            "no_new_privileges": True,
        },
        "lifecycle": {
            "pre_start": [],
            "post_stop": [],
            "stop_timeout_seconds": 30,
        },
        "topology": {
            "name": "solo",
            "mode": "single",
            "node_count": 1,
            "world_size": 1,
            "rank": 0,
            "role": "entrypoint",
            "backend": "local",
        },
        "endpoint": {
            "protocol": "openai",
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
                    "read_only": True,
                },
                "model": {
                    "publisher": "vonk-forge",
                    "slug": "synthetic-tiny-fp16",
                    "content_sha256": "e" * 64,
                },
            }
        ],
    }
    spec["identity"]["execution_sha256"] = execution_identity_sha256(spec)
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


def _image(
    *, source: str = "published", build_id: str | None = None
) -> dict[str, object]:
    layout = "f" * 64
    return {
        "image_digest": "sha256:" + "1" * 64,
        "oci_layout_sha256": layout,
        "image_bytes": 4096,
        "architecture": "linux-arm64",
        "runtime_interface": "vonk.runtime.v1",
        "source": source,
        "build_id": build_id,
        "distribution_object": {
            "name": "image.oci.tar",
            "sha256": layout,
            "bytes": 4096,
            "kind": "oci-archive",
        },
    }


def _compile(
    spec: dict[str, object] | None = None,
    *,
    image: dict[str, object] | None = None,
) -> CompiledExecutionPlan:
    return compile_verified_execution_plan(
        _spec() if spec is None else spec,
        model_artifact_set_sha256="d" * 64,
        model_objects=_model_objects(),
        runtime_image=_image() if image is None else image,
    )


def test_prebuilt_plan_binds_exact_file_and_controller_archive_receipts() -> None:
    plan = _compile()

    artifact = plan.artifacts[0]
    assert artifact.sha256 == _model_objects()[0]["sha256"]
    assert artifact.bytes == len(b"verified model bytes")
    assert artifact.distribution_object.name == "model.safetensors"
    assert artifact.mount.source == "/run/vonk/models/primary"
    assert artifact.materialized_path == "/run/vonk/models/primary/model.safetensors"
    assert artifact.roles == ["entrypoint", "weights"]
    assert plan.runtime_image.source == "published"
    assert plan.runtime_image.distribution_object.kind == "oci-archive"

    payload = plan.to_agent_payload()
    rendered = json.dumps(payload, sort_keys=True)
    assert "repository" not in rendered
    assert "revision" not in rendered
    assert "token" not in rendered
    assert "recipe_revision_sha256" not in payload
    assert "source" not in payload["runtime_image"]
    assert "build_id" not in payload["runtime_image"]


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
        },
    )
    validated = validate_compiled_launch_payload(payload)
    assert set(validated) == {
        "schema_version",
        "identity",
        "runtime",
        "artifacts",
        "runtime_image",
        "security",
        "topology",
        "lifecycle",
        "endpoint",
    }
    assert validated["runtime"]["executable"] == "/opt/vonk/bin/vllm"
    assert validated["runtime"]["argv"] == ["serve"]
    assert validated["artifacts"][0]["selection_id"] == "primary"
    assert validated["artifacts"][0]["mount"] == {
        "target": "/models",
        "read_only": True,
    }
    rendered = json.dumps(validated, sort_keys=True)
    assert "model_version_sha256" not in rendered
    assert "runtime_distribution_sha256" not in rendered
    assert "patch_bundle_sha256" not in rendered


def test_compiled_launch_payload_rejects_retired_authority_or_mismatched_receipt() -> None:
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
        },
    )
    polluted = copy.deepcopy(payload)
    polluted["identity"]["model_version_sha256"] = "f" * 64
    with pytest.raises(CompiledExecutionPlanError, match="identity fields"):
        validate_compiled_launch_payload(polluted)

    mismatched = copy.deepcopy(payload)
    mismatched["artifacts"][0]["distribution_object"]["bytes"] += 1
    with pytest.raises(CompiledExecutionPlanError, match="receipt is inconsistent"):
        validate_compiled_launch_payload(mismatched)


def test_start_claim_binds_live_rank_placement_without_reintroducing_authority() -> None:
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
        },
    )
    started = _compiled_plan_for_start(
        payload,
        node=SimpleNamespace(
            node_id="spk_" + "a" * 32,
            rank=0,
            role="entrypoint",
            port=8000,
            required_memory_bytes=4096,
            fabric_address=None,
        ),
        endpoint_address="192.0.2.10",
        master_address=None,
        master_port=None,
        world_size=1,
    )
    assert started["runtime"]["placement"]["endpoint_address"] == "192.0.2.10"
    assert started["runtime"]["placement"]["reserved_memory_bytes"] == 4096
    assert validate_compiled_launch_payload(started)["schema_version"] == 2


def test_production_agent_spec_route_returns_the_persisted_schema_two_plan(
    tmp_path: Path,
) -> None:
    node_id = "spk_" + "a" * 32
    serial = "serial-a"
    fingerprint = "fingerprint-a"
    now = datetime(2026, 9, 6, tzinfo=UTC)
    engine = create_engine(
        f"sqlite:///{tmp_path / 'agent-spec.sqlite'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    presence = AgentPresenceService(
        sessions,
        ManagementAddressPolicy.parse("10.0.0.0/24"),
        clock=lambda: now,
    )
    operations = AgentJobService(sessions, clock=lambda: now)
    operations.set_contact_consumer(presence.observe_in_session)
    services = AgentApiServices(
        enrollment=None,
        operations=operations,
        sessions=sessions,
        clock=lambda: now,
        presence=presence,
        artifact_root=tmp_path / "artifacts",
        source_bundles=SourceBundleStore(tmp_path / "bundles"),
    )
    services.artifact_root.mkdir()
    spec = _spec()
    payload = _compile(spec).to_compiled_launch_payload(
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
        },
    )
    installation_id = str(uuid4())
    with sessions.begin() as session:
        session.add(AgentNode(node_id=node_id, state="active", capabilities=[]))
        session.add(
            AgentCertificate(
                serial=serial,
                node_id=node_id,
                fingerprint=fingerprint,
                not_before=now - timedelta(days=1),
                not_after=now + timedelta(days=1),
                state="active",
                generation=1,
            )
        )
        session.add(
            RecipeInstallation(
                id=installation_id,
                recipe_revision_id=str(uuid4()),
                mapping_id=str(uuid4()),
                mapping_generation=1,
                recipe_build_id=str(uuid4()),
                image_digest=payload["runtime_image"]["image_digest"],
                plan_digest="a" * 64,
                plan={"compiled_execution_plans": {node_id: payload}},
                state="installed",
                actor="test",
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            InstallationNode(
                installation_id=installation_id,
                node_id=node_id,
                rank=0,
                role="entrypoint",
                state="installed",
                required_bytes=1,
                installed_bytes=1,
                updated_at=now,
            )
        )

    class Jobs:
        def list(self):
            return []

        def get(self, _job_id):
            raise KeyError

        def enqueue(self, *_args, **_kwargs):
            raise AssertionError("the spec route must not enqueue work")

    app = create_app(
        jobs=Jobs(),
        tokens=TokenCodec(b"k" * 32),
        audits=MemoryAuditStore(),
        fleet=dict,
        now=lambda: 0,
        agent=services,
        trusted_agent_proxy_auth=b"p" * 32,
    )
    headers = {
        "x-vonk-agent-node": node_id,
        "x-vonk-agent-serial": serial,
        "x-vonk-agent-fingerprint": fingerprint,
        "x-vonk-agent-verified": "1",
        "x-vonk-agent-proxy-auth": "p" * 32,
        "x-vonk-agent-source": "10.0.0.42",
    }
    with TestClient(app) as client:
        response = client.get(
            f"/agent/v1/recipe-installations/{installation_id}/spec",
            headers=headers,
        )
    assert response.status_code == 200
    assert response.json() == payload
    assert response.json()["schema_version"] == 2
    assert "model_version_sha256" not in response.text
    assert "runtime_distribution_sha256" not in response.text
    assert "patch_bundle_sha256" not in response.text


def test_controller_service_binds_canonical_model_cache_and_build_receipts() -> None:
    pytest.importorskip("vonk_forge_contracts")
    from importlib.resources import files

    from vonk_forge_contracts import ModelDefinition, RecipeDefinition, content_sha256

    recipe = RecipeDefinition.model_validate(
        json.loads(
            files("vonk_forge_contracts")
            .joinpath("examples/recipe-source-build.json")
            .read_text(encoding="utf-8")
        )
    )
    model = ModelDefinition.model_validate(
        json.loads(
            files("vonk_forge_contracts")
            .joinpath("examples/model-definition.json")
            .read_text(encoding="utf-8")
        )
    )
    recipe_document = recipe.model_dump(mode="json")
    model_document = model.model_dump(mode="json")
    model_digest = content_sha256(model)
    recipe_document["models"][0]["model"]["content_sha256"] = model_digest
    recipe = RecipeDefinition.model_validate(recipe_document)
    recipe_digest = content_sha256(recipe)
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
            self, digest: str
        ) -> tuple[dict[str, object], ...]:
            assert digest == artifact_set_digest
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

    node = SimpleNamespace(
        node_id="spk_" + "1" * 32,
        rank=0,
        role="entrypoint",
    )
    revision = SimpleNamespace(
        content_sha256=recipe_digest,
        document=recipe_document,
    )
    build = SimpleNamespace(
        id="build-1",
        state="succeeded",
        image_digest="sha256:" + "1" * 64,
        build_input_sha256="b" * 64,
        oci_layout_sha256="f" * 64,
        image_bytes=4096,
    )
    service = ControllerExecutionPlanService(Cache())
    plans = service.compile_installation(
        None,
        revision=revision,
        build=build,
        mapping_nodes=(node,),
        parameters={},
        resolved_entities={
            "models": (SimpleNamespace(document=model_document, content_digest=model_digest),)
        },
    )

    payload = plans[node.node_id]
    validate_compiled_launch_payload(payload)
    assert payload["identity"]["model_artifact_set_sha256"] == artifact_set_digest
    assert payload["identity"]["model_artifact_bytes"] == 1024
    assert payload["identity"]["build_input_sha256"] == "b" * 64
    assert payload["artifacts"][0]["path"] == "model.safetensors"
    assert payload["runtime_image"]["source"] == "controller-build"
    assert "repository" not in json.dumps(payload, sort_keys=True)


def test_controller_built_receipt_and_pulled_receipt_share_reusable_identity() -> None:
    prebuilt = _compile()
    built = _compile(image=_image(source="controller-build", build_id="build-7"))
    assert built.runtime_image.build_id == "build-7"
    assert built.reusable_identity_sha256 == prebuilt.reusable_identity_sha256

    editorial = _compile(_spec(recipe_digest="9" * 64))
    assert editorial.recipe_revision_sha256 != prebuilt.recipe_revision_sha256
    assert editorial.reusable_identity_sha256 == prebuilt.reusable_identity_sha256


def test_mount_change_invalidates_reuse_identity_without_changing_bytes() -> None:
    first = _compile()
    changed = _compile(_spec(mount_target="/models/alternate"))

    assert changed.artifacts[0].sha256 == first.artifacts[0].sha256
    assert changed.artifacts[0].bytes == first.artifacts[0].bytes
    assert changed.reusable_identity_sha256 != first.reusable_identity_sha256


def test_selector_label_change_does_not_invalidate_reusable_bytes() -> None:
    first = _compile()
    changed_spec = _spec()
    changed_spec["artifacts"][0]["id"] = "release-label"

    changed = _compile(changed_spec)
    assert changed.artifacts[0].id == "release-label"
    assert changed.reusable_identity_sha256 == first.reusable_identity_sha256


def test_upstream_authority_cannot_enter_compiled_receipts() -> None:
    polluted = _spec()
    model = polluted["artifacts"][0]["model"]
    assert isinstance(model, dict)
    model["repository"] = "huggingface.co/private/model"

    with pytest.raises(CompiledExecutionPlanError, match="upstream authority"):
        _compile(polluted)


def test_retired_runtime_authority_cannot_enter_compiled_receipts() -> None:
    polluted = _spec()
    polluted["identity"]["model_version_sha256"] = "f" * 64

    with pytest.raises(CompiledExecutionPlanError, match="retired authority"):
        _compile(polluted)


def test_mismatched_distribution_receipt_is_rejected() -> None:
    plan = _compile()
    artifact = plan.artifacts[0].model_dump(mode="json")
    artifact["distribution_object"]["bytes"] += 1

    with pytest.raises(ValidationError, match="bytes do not match"):
        CompiledModelArtifact.model_validate(artifact)


def test_controller_build_requires_build_id_and_exact_archive_identity() -> None:
    with pytest.raises(CompiledExecutionPlanError, match="verified runtime image"):
        _compile(image=_image(source="controller-build"))

    image = _image()
    image["distribution_object"]["sha256"] = "2" * 64
    with pytest.raises(CompiledExecutionPlanError, match="verified runtime image"):
        _compile(image=image)


def test_plan_rejects_incomplete_selected_cache_receipt() -> None:
    objects = _model_objects()
    objects[0]["path"] = "config.json"
    objects[0]["distribution_object"]["name"] = "config.json"
    with pytest.raises(CompiledExecutionPlanError, match="path, digest"):
        compile_verified_execution_plan(
            _spec(),
            model_artifact_set_sha256="d" * 64,
            model_objects=objects,
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
    spec["identity"]["execution_sha256"] = "0" * 64
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
                "read_only": True,
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
                "read_only": True,
            },
            "model": {
                "publisher": "radixark",
                "slug": "qwen3-8-27b-dspark-b3c99101",
                "content_sha256": "4091ffe98645f39f163c52efe1228f5385970df1d631df050eea1628b6721888",
            },
        },
    ]
    spec["identity"]["execution_sha256"] = execution_identity_sha256(spec)
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
    with pytest.raises(ValidationError, match="materialized path"):
        CompiledExecutionPlan.model_validate(document)


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
    spec["artifacts"][0].update(
        {
            "id": "tokenizer-config",
            "file_id": "tokenizer-config",
            "path": "tokenizer_config.json",
            "sha256": EMPTY_SHA256,
            "bytes": 0,
            "roles": ["auxiliary"],
        }
    )
    spec["identity"]["execution_sha256"] = execution_identity_sha256(spec)
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

    spec["artifacts"][0]["sha256"] = "a" * 64
    spec["identity"]["execution_sha256"] = execution_identity_sha256(spec)
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
    notes["model_dependencies"][0]["artifact_key"] = "new-provenance-handle"
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
        changed[key].update(value)
        changes.append(execution_identity_sha256(changed))
    changed_artifact = copy.deepcopy(base)
    changed_artifact["artifacts"][0]["mount"]["target"] = "/models/changed"
    changes.append(execution_identity_sha256(changed_artifact))

    assert all(value != baseline for value in changes)
    assert len(set(changes)) == len(changes)
