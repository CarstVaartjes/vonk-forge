from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import pytest
from vonk_agent_protocol import (
    AgentDirective,
    AgentOperation,
    AgentProtocolError,
    PackageOperationRequest,
    canonical_message,
    validate_schema_message,
)

RELEASE_PAYLOAD = {
    "schema_version": 1,
    "deployment_id": "future-stack",
    "release_digest": "a" * 64,
    "deployment_digest": "b" * 64,
}


def directive_document() -> dict[str, object]:
    return {
        "schema_version": 1,
        "job_id": "00000000-0000-4000-8000-000000000001",
        "operation_id": "00000000-0000-4000-8000-000000000002",
        "attempt": 1,
        "fence": "00000000-0000-4000-8000-000000000003",
        "node_id": "spk_00000000000000000000000000000001",
        "deadline": "2026-08-06T12:00:00+00:00",
        "cancel_requested": False,
    }


def package_claim_document(
    operation: str, payload: dict[str, object]
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "job_id": "00000000-0000-4000-8000-000000000001",
        "operation_id": "00000000-0000-4000-8000-000000000002",
        "attempt": 1,
        "fence": "00000000-0000-4000-8000-000000000003",
        "node_id": "spk_00000000000000000000000000000001",
        "operation": operation,
        "base_commit": "c" * 40,
        "payload_digest": hashlib.sha256(canonical_message(payload)).hexdigest(),
        "payload": payload,
        "deadline": "2026-08-06T12:00:00+00:00",
    }


def test_package_operation_vocabulary_is_generic_and_closed() -> None:
    assert {operation.value for operation in AgentOperation if operation.value.startswith("package.")} == {
        "package.prepare",
        "package.activate",
        "package.health",
        "package.stop",
        "package.rollback",
        "package.remove",
        "package.repair",
        "package.gc",
    }


@pytest.mark.parametrize(
    "operation",
    [
        AgentOperation.PACKAGE_PREPARE,
        AgentOperation.PACKAGE_ACTIVATE,
        AgentOperation.PACKAGE_HEALTH,
        AgentOperation.PACKAGE_STOP,
        AgentOperation.PACKAGE_ROLLBACK,
        AgentOperation.PACKAGE_REMOVE,
        AgentOperation.PACKAGE_REPAIR,
    ],
)
def test_release_bound_package_operations_accept_an_unknown_deployment_family(
    operation: AgentOperation,
) -> None:
    request = PackageOperationRequest.parse(operation, RELEASE_PAYLOAD)

    assert request.operation is operation
    assert request.deployment_id == "future-stack"
    assert request.release_digest == "a" * 64
    assert request.deployment_digest == "b" * 64


def test_release_bound_package_operations_carry_signed_deployment_execution_policy() -> None:
    deployment = {
        "schema_version": 1,
        "deployment_id": "future-stack",
        "family_id": "synthetic-family",
        "release_digest": "a" * 64,
        "selector": {"node_count": 1, "required_labels": {}, "preferred_node_ids": []},
        "secrets": {},
        "ports": {"http": 8080},
        "arguments": ["serve", "--port", "8080"],
        "routing": {"alias": "future", "port": "http"},
        "resources": {"memory_bytes": 4096, "storage_bytes": 8192, "gpu_count": 1},
    }
    deployment_digest = hashlib.sha256(canonical_message(deployment) + b"\n").hexdigest()
    payload = (RELEASE_PAYLOAD | {"deployment_digest": deployment_digest}) | {
        "deployment": deployment,
        "deployment_config_digest": deployment_digest,
    }

    request = PackageOperationRequest.parse(AgentOperation.PACKAGE_PREPARE, payload)

    assert request.deployment is not None
    assert request.deployment["arguments"] == ("serve", "--port", "8080")
    assert request.deployment["resources"]["memory_bytes"] == 4096


def test_deployment_secret_references_are_allowed_but_paths_are_not() -> None:
    deployment = {
        "schema_version": 1,
        "deployment_id": "future-stack",
        "family_id": "synthetic-family",
        "release_digest": "a" * 64,
        "selector": {"node_count": 1, "required_labels": {}, "preferred_node_ids": []},
        "secrets": {"hf_token": "secret://workload/hf-token"},
        "ports": {"http": 8080},
        "arguments": ["serve"],
        "routing": {"alias": "future", "port": "http"},
        "resources": {"memory_bytes": 4096, "storage_bytes": 8192, "gpu_count": 1},
    }
    digest = hashlib.sha256(canonical_message(deployment) + b"\n").hexdigest()
    payload = RELEASE_PAYLOAD | {
        "deployment": deployment,
        "deployment_digest": digest,
        "deployment_config_digest": digest,
    }
    assert PackageOperationRequest.parse(AgentOperation.PACKAGE_PREPARE, payload).deployment
    validate_schema_message(
        "agent-job.schema.json",
        package_claim_document("package.prepare", payload),
    )


def test_release_bound_package_operations_reject_tampered_deployment_projection() -> None:
    deployment = {
        "schema_version": 1,
        "deployment_id": "future-stack",
        "family_id": "synthetic-family",
        "release_digest": "a" * 64,
        "selector": {"node_count": 1, "required_labels": {}, "preferred_node_ids": []},
        "secrets": {},
        "ports": {"http": 8080},
        "arguments": ["serve"],
        "routing": {"alias": "future", "port": "http"},
        "resources": {"memory_bytes": 4096, "storage_bytes": 8192, "gpu_count": 1},
    }
    payload = RELEASE_PAYLOAD | {
        "deployment": deployment,
        "deployment_digest": hashlib.sha256(canonical_message(deployment) + b"\n").hexdigest(),
        "deployment_config_digest": hashlib.sha256(canonical_message(deployment) + b"\n").hexdigest(),
    }
    deployment["arguments"] = ["tampered"]

    with pytest.raises(AgentProtocolError, match="deployment digest"):
        PackageOperationRequest.parse(AgentOperation.PACKAGE_PREPARE, payload)


def test_package_gc_has_its_own_bounded_closed_payload() -> None:
    request = PackageOperationRequest.parse(
        AgentOperation.PACKAGE_GC,
        {"schema_version": 1, "dry_run": True, "target_bytes": 1024**4},
    )

    assert request.deployment_id is None
    assert request.dry_run is True
    assert request.target_bytes == 1024**4

    with pytest.raises(AgentProtocolError, match="target_bytes"):
        PackageOperationRequest.parse(
            AgentOperation.PACKAGE_GC,
            {"schema_version": 1, "dry_run": False, "target_bytes": 0},
        )
    with pytest.raises(AgentProtocolError, match="target_bytes"):
        PackageOperationRequest.parse(
            AgentOperation.PACKAGE_GC,
            {
                "schema_version": 1,
                "dry_run": False,
                "target_bytes": 16 * 1024**4 + 1,
            },
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("deployment_id", "FutureStack"),
        ("deployment_id", "future/stack"),
        ("release_digest", "sha256:" + "a" * 64),
        ("release_digest", "A" * 64),
        ("deployment_digest", "b" * 63),
    ],
)
def test_package_operation_rejects_invalid_identifiers_and_digests(
    field: str, value: object
) -> None:
    with pytest.raises(AgentProtocolError):
        PackageOperationRequest.parse(
            AgentOperation.PACKAGE_PREPARE,
            RELEASE_PAYLOAD | {field: value},
        )


@pytest.mark.parametrize("field", ["command", "artifact_path", "secret", "adapter_id"])
def test_package_operation_rejects_commands_paths_secrets_and_adapter_catalog_fields(
    field: str,
) -> None:
    with pytest.raises(AgentProtocolError, match="fields"):
        PackageOperationRequest.parse(
            AgentOperation.PACKAGE_PREPARE,
            RELEASE_PAYLOAD | {field: "untrusted"},
        )


def test_package_operation_rejects_cross_operation_fields_and_unknown_operations() -> None:
    with pytest.raises(AgentProtocolError, match="fields"):
        PackageOperationRequest.parse(
            AgentOperation.PACKAGE_PREPARE,
            RELEASE_PAYLOAD | {"dry_run": True},
        )
    with pytest.raises(AgentProtocolError, match="fields"):
        PackageOperationRequest.parse(
            AgentOperation.PACKAGE_GC,
            RELEASE_PAYLOAD | {"dry_run": True},
        )
    with pytest.raises(AgentProtocolError, match="operation"):
        PackageOperationRequest.parse("package.future", RELEASE_PAYLOAD)  # type: ignore[arg-type]


def test_job_schema_applies_exact_operation_specific_package_payloads() -> None:
    assert validate_schema_message(
        "agent-job.schema.json",
        package_claim_document("package.prepare", RELEASE_PAYLOAD),
    ).operation is AgentOperation.PACKAGE_PREPARE
    assert validate_schema_message(
        "agent-job.schema.json",
        package_claim_document(
            "package.gc",
            {"schema_version": 1, "dry_run": True, "target_bytes": 4096},
        ),
    ).operation is AgentOperation.PACKAGE_GC

    with pytest.raises(AgentProtocolError, match="schema"):
        validate_schema_message(
            "agent-job.schema.json",
            package_claim_document(
                "package.prepare",
                RELEASE_PAYLOAD | {"adapter_id": "compiled-catalog-entry"},
            ),
        )
    with pytest.raises(AgentProtocolError, match="schema"):
        validate_schema_message(
            "agent-job.schema.json",
            package_claim_document(
                "package.gc",
                {"schema_version": 1, "dry_run": True, "release_digest": "a" * 64},
            ),
        )


def test_agent_directive_is_exact_fenced_and_schema_validated() -> None:
    directive = AgentDirective.parse(directive_document())

    assert directive.deadline == datetime(2026, 8, 6, 12, tzinfo=UTC)
    assert directive.cancel_requested is False
    assert validate_schema_message("agent-directive.schema.json", directive_document()) == directive
    assert b'"cancel_requested":false' in canonical_message(directive)


@pytest.mark.parametrize(
    "change",
    [
        {"cancel_requested": 0},
        {"deadline": "2026-08-06T12:00:00+02:00"},
        {"fence": "not-a-fence"},
        {"command": "stop-now"},
    ],
)
def test_agent_directive_rejects_malformed_or_extra_fields(
    change: dict[str, object],
) -> None:
    with pytest.raises(AgentProtocolError):
        AgentDirective.parse(directive_document() | change)
