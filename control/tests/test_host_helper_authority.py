from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from vonk_agent_protocol import (
    ContainerRuntimeAction,
    HostHelperOperation,
    HostOperationKind,
    host_helper_grant_signing_bytes,
)
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
        operation=HostHelperOperation(
            HostOperationKind.RESTART_VONK_UNIT, {"unit": "agent"}
        ),
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
        operation=HostHelperOperation(
            HostOperationKind.EXECUTE_CONTAINER_RUNTIME_REQUEST,
            {
                "action": ContainerRuntimeAction.START.value,
                "job_id": "20000000-0000-4000-8000-000000000002",
                "operation_id": "30000000-0000-4000-8000-000000000003",
                "attempt": 2,
                "fence": "40000000-0000-4000-8000-000000000004",
                "request_sha256": "a" * 64,
            },
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
            operation=HostHelperOperation(
                HostOperationKind.SCHEDULE_REBOOT, {"delay_seconds": 120}
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


def runtime_service(*, lease_seconds: int = 60) -> HostRuntimeAuthorityService:
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
                kind="recipe.start",
                state="running",
                actor="admin",
                base_commit="b" * 40,
                targets=[node_id],
                payload_digest="c" * 64,
                payload={},
                created_at=NOW,
                updated_at=NOW,
            )
        )
        session.add(
            AgentOperation(
                id=operation_id,
                parent_job_id=job_id,
                node_id=node_id,
                kind="recipe.start",
                payload_digest="d" * 64,
                payload={},
                base_commit="b" * 40,
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

    assert grant.claims.operation.values["request_sha256"] == "e" * 64
    assert grant.claims.operation.values["action"] == "start"

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
    assert inspect.claims.operation.values["action"] == "run-inspect"


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
