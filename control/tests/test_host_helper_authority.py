from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import TypedDict

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from vonk_agent_protocol import (
    ContainerRuntimeAction,
    ExecuteContainerRuntimeRequestOperation,
    RestartVonkUnitOperation,
    ScheduleRebootOperation,
    canonical_message,
    host_helper_grant_signing_bytes,
)
from vonk_agent_protocol.host_helper import HostRuntimeRequest
from vonk_agent_protocol.recipe_operations import (
    RecipeUninstallPayload,
)
from vonk_control.agent_api import HostRuntimeGrantRequest
from vonk_control.host_helper_authority import (
    HostHelperAuthorityError,
    HostHelperGrantIssuer,
    HostRuntimeAuthorityService,
)
from vonk_control.models import (
    AgentCertificate,
    AgentNode,
    AgentOperation,
    AgentOperationAttempt,
    Base,
    Job,
)

NOW = datetime(2036, 7, 1, 12, 0, tzinfo=UTC)
REQUEST_ID = "10000000-0000-4000-8000-000000000001"


def issuer() -> HostHelperGrantIssuer:
    return HostHelperGrantIssuer(
        ed25519.Ed25519PrivateKey.from_private_bytes(b"m" * 32),
        clock=lambda: NOW,
        request_id_factory=lambda: REQUEST_ID,
    )


def test_controller_issues_exact_short_lived_host_grant() -> None:
    authority = issuer()
    grant = authority.issue_grant(
        node_id="spk_" + "1" * 32,
        operation=RestartVonkUnitOperation(type="restart-vonk-unit", unit="agent"),
        expires_in_seconds=90,
    )

    assert grant.claims.request_id == REQUEST_ID
    assert grant.claims.expires_at - grant.claims.issued_at == 90
    authority.public_key.verify(
        bytes.fromhex(grant.signature.value),
        host_helper_grant_signing_bytes(grant.claims),
    )
    assert authority.public_key_document()["usage"] == "host-maintenance-grant"


def test_controller_signs_exact_job_bound_container_runtime_request() -> None:
    authority = issuer()
    grant = authority.issue_grant(
        node_id="spk_" + "1" * 32,
        operation=ExecuteContainerRuntimeRequestOperation(
            type="execute-container-runtime-request",
            action=ContainerRuntimeAction.START.value,
            job_id="20000000-0000-4000-8000-000000000002",
            operation_id="30000000-0000-4000-8000-000000000003",
            attempt=2,
            fence="40000000-0000-4000-8000-000000000004",
            request_sha256="a" * 64,
        ),
        expires_in_seconds=30,
    )

    assert grant.claims.operation.to_mapping() == {
        "type": "execute-container-runtime-request",
        "action": "start",
        "job_id": "20000000-0000-4000-8000-000000000002",
        "operation_id": "30000000-0000-4000-8000-000000000003",
        "attempt": 2,
        "fence": "40000000-0000-4000-8000-000000000004",
        "request_sha256": "a" * 64,
    }


@pytest.mark.parametrize("seconds", (0, 301, True))
def test_controller_refuses_unbounded_host_grants(seconds: object) -> None:
    with pytest.raises(HostHelperAuthorityError, match="expiry"):
        issuer().issue_grant(
            node_id="spk_" + "1" * 32,
            operation=ScheduleRebootOperation(
                type="schedule-reboot", delay_seconds=120
            ),
            expires_in_seconds=seconds,
        )


def test_controller_refuses_mapping_shaped_or_untyped_operations() -> None:
    with pytest.raises(HostHelperAuthorityError, match="operation"):
        issuer().issue_grant(
            node_id="spk_" + "1" * 32,
            operation={"type": "restart-vonk-unit", "unit": "agent"},
            expires_in_seconds=30,
        )


def runtime_service(
    *,
    lease_seconds: int = 60,
    operation_kind: str = "recipe.start",
    operation_payload: dict[str, object] | None = None,
    cancel_requested: bool = False,
) -> HostRuntimeAuthorityService:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    node_id = "spk_" + "1" * 32
    job_id = "20000000-0000-4000-8000-000000000002"
    operation_id = "30000000-0000-4000-8000-000000000003"
    fence = "40000000-0000-4000-8000-000000000004"
    with sessions.begin() as session:
        session.add(AgentNode(node_id=node_id, state="active", capabilities=[]))
        session.add(
            AgentCertificate(
                serial="certificate-1",
                node_id=node_id,
                not_before=NOW,
                not_after=NOW,
                fingerprint="fingerprint-1",
            )
        )
        session.add(
            Job(
                id=job_id,
                request_id="50000000-0000-4000-8000-000000000005",
                kind=operation_kind,
                state="running",
                actor="admin",
                authority_revision="b" * 64,
                targets=[node_id],
                payload_digest="c" * 64,
                payload={},
                result={"cancel_requested": True} if cancel_requested else None,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.add(
            AgentOperation(
                id=operation_id,
                parent_job_id=job_id,
                node_id=node_id,
                kind=operation_kind,
                payload_digest="d" * 64,
                payload=operation_payload or {},
                authority_revision="b" * 64,
                state="running",
                current_attempt=2,
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.add(
            AgentOperationAttempt(
                id="60000000-0000-4000-8000-000000000006",
                operation_id=operation_id,
                attempt=2,
                fence=fence,
                lease_deadline=NOW + timedelta(seconds=lease_seconds),
                agent_certificate_serial="certificate-1",
                state="running",
            )
        )
    return HostRuntimeAuthorityService(sessions, issuer(), clock=lambda: NOW)


def test_agent_upgrade_authority_binds_the_live_attempt_and_exact_signed_package() -> (
    None
):
    package = upgrade_payload()
    service = runtime_service(
        operation_kind="agent.upgrade.v1",
        operation_payload=package,
    )

    grant = service.issue_agent_upgrade_grant(
        node_id="spk_" + "1" * 32,
        job_id="20000000-0000-4000-8000-000000000002",
        operation_id="30000000-0000-4000-8000-000000000003",
        attempt=2,
        fence="40000000-0000-4000-8000-000000000004",
        package_sha256=package["package_sha256"],
        package_signature=package["package_signature"],
        certificate_serial="certificate-1",
        expires_in_seconds=30,
    )

    assert grant.claims.operation.to_mapping() == {
        "type": "install-vonk-deb",
        "package_sha256": package["package_sha256"],
        "package_signature": package["package_signature"],
        "rollback": package["rollback"],
    }
    with pytest.raises(HostHelperAuthorityError, match="stale"):
        service.issue_agent_upgrade_grant(
            node_id="spk_" + "1" * 32,
            job_id="20000000-0000-4000-8000-000000000002",
            operation_id="30000000-0000-4000-8000-000000000003",
            attempt=2,
            fence="40000000-0000-4000-8000-000000000004",
            package_sha256="c" * 64,
            package_signature=package["package_signature"],
            certificate_serial="certificate-1",
            expires_in_seconds=30,
        )


def test_runtime_authority_binds_active_attempt_action_and_request() -> None:
    service = runtime_service()
    grant = service.issue_grant(
        node_id="spk_" + "1" * 32,
        job_id="20000000-0000-4000-8000-000000000002",
        operation_id="30000000-0000-4000-8000-000000000003",
        attempt=2,
        fence="40000000-0000-4000-8000-000000000004",
        action=ContainerRuntimeAction.START,
        request_sha256="e" * 64,
        certificate_serial="certificate-1",
    )

    operation = grant.claims.operation
    assert isinstance(operation, ExecuteContainerRuntimeRequestOperation)
    assert operation.request_sha256 == "e" * 64
    assert operation.action == "start"

    inspect = service.issue_grant(
        node_id="spk_" + "1" * 32,
        job_id="20000000-0000-4000-8000-000000000002",
        operation_id="30000000-0000-4000-8000-000000000003",
        attempt=2,
        fence="40000000-0000-4000-8000-000000000004",
        action=ContainerRuntimeAction.RUN_INSPECT,
        request_sha256="f" * 64,
        certificate_serial="certificate-1",
    )
    inspect_operation = inspect.claims.operation
    assert isinstance(inspect_operation, ExecuteContainerRuntimeRequestOperation)
    assert inspect_operation.action == "run-inspect"


def test_collective_readiness_grant_is_strictly_inspect_only() -> None:
    service = runtime_service(operation_payload={"phase": "collective-readiness"})
    common = {
        "node_id": "spk_" + "1" * 32,
        "job_id": "20000000-0000-4000-8000-000000000002",
        "operation_id": "30000000-0000-4000-8000-000000000003",
        "attempt": 2,
        "fence": "40000000-0000-4000-8000-000000000004",
        "request_sha256": "e" * 64,
        "certificate_serial": "certificate-1",
    }
    inspected = service.issue_grant(
        **common, action=ContainerRuntimeAction.RUN_INSPECT
    ).claims.operation
    assert isinstance(inspected, ExecuteContainerRuntimeRequestOperation)
    assert inspected.action == "run-inspect"
    for action in (ContainerRuntimeAction.START, ContainerRuntimeAction.STOP):
        with pytest.raises(HostHelperAuthorityError, match="stale"):
            service.issue_grant(**common, action=action)


@pytest.mark.parametrize(
    "action", (ContainerRuntimeAction.START, ContainerRuntimeAction.STOP)
)
def test_job_run_cancellation_keeps_exact_start_and_stop_authority_live(
    action: ContainerRuntimeAction,
) -> None:
    service = runtime_service(operation_kind="recipe.job.run.v1", cancel_requested=True)

    grant = service.issue_grant(
        node_id="spk_" + "1" * 32,
        job_id="20000000-0000-4000-8000-000000000002",
        operation_id="30000000-0000-4000-8000-000000000003",
        attempt=2,
        fence="40000000-0000-4000-8000-000000000004",
        action=action,
        request_sha256="e" * 64,
        certificate_serial="certificate-1",
    )

    operation = grant.claims.operation
    assert isinstance(operation, ExecuteContainerRuntimeRequestOperation)
    assert operation.action == action.value


def test_runtime_authority_rejects_action_not_owned_by_active_operation() -> None:
    with pytest.raises(HostHelperAuthorityError, match="action"):
        runtime_service().issue_grant(
            node_id="spk_" + "1" * 32,
            job_id="20000000-0000-4000-8000-000000000002",
            operation_id="30000000-0000-4000-8000-000000000003",
            attempt=2,
            fence="40000000-0000-4000-8000-000000000004",
            action=ContainerRuntimeAction.IMAGE_IMPORT,
            request_sha256="e" * 64,
            certificate_serial="certificate-1",
        )


def test_runtime_preflight_grant_is_bound_to_its_own_fenced_operation() -> None:
    arguments = {
        "node_id": "spk_" + "1" * 32,
        "job_id": "20000000-0000-4000-8000-000000000002",
        "operation_id": "30000000-0000-4000-8000-000000000003",
        "attempt": 2,
        "fence": "40000000-0000-4000-8000-000000000004",
        "action": ContainerRuntimeAction.RUNTIME_PREFLIGHT,
        "request_sha256": "e" * 64,
        "certificate_serial": "certificate-1",
    }
    service = runtime_service(operation_kind="runtime.preflight.v1")
    grant = service.issue_grant(**arguments)
    operation = grant.claims.operation
    assert isinstance(operation, ExecuteContainerRuntimeRequestOperation)
    assert operation.action == "runtime-preflight"
    assert operation.request_sha256 == "e" * 64
    for changes in [{"fence": "50000000-0000-4000-8000-000000000005"}, {"action": ContainerRuntimeAction.START}]:
        with pytest.raises(HostHelperAuthorityError):
            service.issue_grant(**{**arguments, **changes})
    with pytest.raises(HostHelperAuthorityError):
        runtime_service(operation_kind="recipe.start").issue_grant(**arguments)


def test_runtime_authority_never_issues_a_grant_past_the_attempt_lease() -> None:
    with pytest.raises(HostHelperAuthorityError, match="lease"):
        runtime_service(lease_seconds=10).issue_grant(
            node_id="spk_" + "1" * 32,
            job_id="20000000-0000-4000-8000-000000000002",
            operation_id="30000000-0000-4000-8000-000000000003",
            attempt=2,
            fence="40000000-0000-4000-8000-000000000004",
            action=ContainerRuntimeAction.START,
            request_sha256="e" * 64,
            certificate_serial="certificate-1",
            expires_in_seconds=30,
        )


def upgrade_payload():
    return {"schema_version": 1, "architecture": "linux-arm64", "package_sha256": "a" * 64,
        "package_signature": "b" * 128, "package_bytes": 1000, "package_version": "0.1.2",
        "package_url": "https://install.vonkforge.ai/artifacts/candidate/vonk-forge-agent.deb",
        "target_binary_digest": "c" * 64, "target_build_digest": "sha256:" + "d" * 64,
        "source_package_url": "https://install.vonkforge.ai/artifacts/source/vonk-forge-agent.deb",
        "source_package_bytes": 999,
        "rollback": {"attempt_nonce": "e" * 64, "activation_deadline": int(NOW.timestamp()) + 900,
            "source": {"package_sha256": "f" * 64, "package_signature": "1" * 128,
                "package_version": "0.1.1", "binary_sha256": "2" * 64, "helper_sha256": "3" * 64}}}


def test_activation_grant_is_bound_to_live_source_candidate_nonce_and_identity():
    from vonk_agent_protocol.claims import AgentRuntimeIdentity
    from vonk_agent_protocol.package_upgrade import PackageActivationReceipt
    payload = upgrade_payload()
    service = runtime_service(operation_kind="agent.upgrade.v1", operation_payload=payload)
    receipt = PackageActivationReceipt(schema_version=2, node_id="spk_" + "1" * 32,
        source_package_sha256="f" * 64, source_version="0.1.1", source_binary_sha256="2" * 64,
        candidate_package_sha256="a" * 64, candidate_version="0.1.2", candidate_binary_sha256="c" * 64,
        attempt_nonce="e" * 64, phase="armed", created_at=int(NOW.timestamp()), updated_at=int(NOW.timestamp()), outcome="watchdog_armed")
    identity = AgentRuntimeIdentity(architecture="linux-arm64", semantic_version="0.1.2",
        build_digest="sha256:" + "d" * 64, binary_digest="c" * 64, self_test_passed=True,
        observation_receipt_public_key="4" * 64)
    grant = service.issue_package_activation_grant(node_id=receipt.node_id, receipt=receipt, runtime_identity=identity, certificate_serial="certificate-1")
    assert grant.claims.operation.to_mapping() == {"type": "confirm-package-activation", "package_sha256": "a" * 64, "attempt_nonce": "e" * 64}
    issuer().public_key.verify(bytes.fromhex(grant.signature.value), host_helper_grant_signing_bytes(grant.claims))
    for changed in ({"attempt_nonce": "0" * 64}, {"source_package_sha256": "0" * 64}, {"phase": "rolled_back"}, {"candidate_binary_sha256": "0" * 64}):
        invalid = PackageActivationReceipt.model_validate({**receipt.model_dump(mode="json"), **changed})
        with pytest.raises(HostHelperAuthorityError):
            service.issue_package_activation_grant(node_id=receipt.node_id, receipt=invalid, runtime_identity=identity, certificate_serial="certificate-1")
    with pytest.raises(HostHelperAuthorityError):
        service.issue_package_activation_grant(node_id=receipt.node_id, receipt=receipt, runtime_identity=identity.model_copy(update={"binary_digest": "0" * 64}), certificate_serial="certificate-1")


INSTALLATION_ID = "70000000-0000-4000-8000-000000000007"
SECOND_INSTALLATION_ID = "80000000-0000-4000-8000-000000000008"
OUTSIDE_INSTALLATION_ID = "90000000-0000-4000-8000-000000000009"


class _CleanupGrantArguments(TypedDict):
    node_id: str
    job_id: str
    operation_id: str
    attempt: int
    fence: str
    action: ContainerRuntimeAction
    request_sha256: str
    certificate_serial: str
    installation_id: str | None


def cleanup_grant_arguments(installation_id: str) -> _CleanupGrantArguments:
    request = HostRuntimeRequest(
        schema_version=1,
        action="installation-cleanup",
        job_id="20000000-0000-4000-8000-000000000002",
        operation_id="30000000-0000-4000-8000-000000000003",
        attempt=2,
        fence="40000000-0000-4000-8000-000000000004",
        arguments=[],
        installation_id=installation_id,
    )
    return {
        "node_id": "spk_" + "1" * 32,
        "job_id": request.job_id,
        "operation_id": request.operation_id,
        "attempt": request.attempt,
        "fence": request.fence,
        "action": ContainerRuntimeAction.INSTALLATION_CLEANUP,
        "request_sha256": hashlib.sha256(canonical_message(request)).hexdigest(),
        "certificate_serial": "certificate-1",
        "installation_id": installation_id,
    }


def test_cleanup_grants_bind_only_installations_in_canonical_operation_payload(
) -> None:
    operation_kind = "recipe.uninstall"
    payload = RecipeUninstallPayload(
        schema_version=1,
        installation_id=INSTALLATION_ID,
        plan_digest="a" * 64,
        recipe_content_sha256="b" * 64,
        cleanup_model_content_sha256=None,
    )
    authorized = [INSTALLATION_ID]
    unauthorized = [SECOND_INSTALLATION_ID, OUTSIDE_INSTALLATION_ID]
    service = runtime_service(
        operation_kind=operation_kind,
        operation_payload=json.loads(canonical_message(payload)),
    )
    for installation_id in authorized:
        arguments = cleanup_grant_arguments(installation_id)
        grant = service.issue_grant(**arguments)
        operation = grant.claims.operation
        assert isinstance(operation, ExecuteContainerRuntimeRequestOperation)
        assert operation.installation_id == installation_id
        assert operation.action == "installation-cleanup"
        assert operation.request_sha256 == arguments["request_sha256"]
        issuer().public_key.verify(
            bytes.fromhex(grant.signature.value),
            host_helper_grant_signing_bytes(grant.claims),
        )
    for installation_id in unauthorized:
        with pytest.raises(HostHelperAuthorityError, match="installation is unauthorized"):
            service.issue_grant(**cleanup_grant_arguments(installation_id))


def test_runtime_authority_rejects_installation_binding_on_noncleanup_action() -> None:
    arguments = cleanup_grant_arguments(INSTALLATION_ID)
    arguments["action"] = ContainerRuntimeAction.START
    with pytest.raises(HostHelperAuthorityError, match="installation binding is invalid"):
        runtime_service().issue_grant(**arguments)


def test_cleanup_authority_rejects_malformed_persisted_payload() -> None:
    payload = {
        "schema_version": 1,
        "installation_id": INSTALLATION_ID,
        "plan_digest": "a" * 64,
        "recipe_content_sha256": "b" * 64,
    }
    # The required nullable cleanup field cannot disappear from stored authority.
    service = runtime_service(operation_kind="recipe.uninstall", operation_payload=payload)
    with pytest.raises(HostHelperAuthorityError, match="cleanup authority is invalid"):
        service.issue_grant(**cleanup_grant_arguments(INSTALLATION_ID))


def test_runtime_grant_request_enforces_cleanup_identity_and_null_policy() -> None:
    document: dict[str, object] = {}
    document.update(cleanup_grant_arguments(INSTALLATION_ID))
    document.pop("certificate_serial")
    document["action"] = "installation-cleanup"
    document["expires_in_seconds"] = 30
    assert HostRuntimeGrantRequest.model_validate(document).installation_id == INSTALLATION_ID
    for missing in ({}, {"installation_id": None}):
        incomplete = {key: value for key, value in document.items() if key != "installation_id"}
        with pytest.raises(ValidationError, match="installation binding"):
            HostRuntimeGrantRequest.model_validate(incomplete | missing)
    ordinary = document | {"action": "start"}
    with pytest.raises(ValidationError, match="installation binding"):
        HostRuntimeGrantRequest.model_validate(ordinary)
    ordinary.pop("installation_id")
    omitted = HostRuntimeGrantRequest.model_validate(ordinary)
    explicit_null = HostRuntimeGrantRequest.model_validate(ordinary | {"installation_id": None})
    assert canonical_message(omitted) == canonical_message(explicit_null)
    assert "installation_id" not in json.loads(canonical_message(explicit_null))
