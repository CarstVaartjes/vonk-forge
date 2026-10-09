"""Controller-only signer for the narrow GPU node host-maintenance helper."""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar
from uuid import uuid4

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import AgentOperation, AgentProtocolError, canonical_message
from vonk_agent_protocol.claims import AgentRuntimeIdentity
from vonk_agent_protocol.contracts import AgentUpgradePayload
from vonk_agent_protocol.host_helper import (
    HOST_HELPER_AUTHORITY,
    MAX_HOST_HELPER_GRANT_SECONDS,
    ConfirmPackageActivationOperation,
    ContainerRuntimeAction,
    ExecuteContainerRuntimeRequestOperation,
    HostHelperGrantClaims,
    HostHelperSignature,
    HostOperationKind,
    HostRuntimeRequest,
    InstallVonkDebOperation,
    RecipeReconciliationIdentity,
    SignedHostHelperGrant,
    host_helper_grant_signing_bytes,
)
from vonk_agent_protocol.package_upgrade import PackageActivationReceipt
from vonk_agent_protocol.recipe_operations import (
    RecipeReconcilePayload,
    RecipeUninstallPayload,
)

from . import agent_operation_states
from .agent_jobs import (
    _WORKLOAD_INTENT_OPERATIONS,
    superseded_cancellation_deadline,
)
from .host_runtime_plan_authority import (
    RuntimePlanAuthorityError,
    RuntimePlanBinding,
    derive_runtime_plan_binding,
)
from .models import (
    AgentCertificate,
    AgentNode,
    AgentOperationAttempt,
    Job,
)
from .models import AgentOperation as StoredAgentOperation
from .offline_stops import is_deferred_stop
from .package_activation import matches_receipt
from .strict_json import read_stored_model


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class HostHelperAuthorityError(RuntimeError):
    """The host-helper grant could not be issued safely."""


def _load_private_key(path: Path) -> ed25519.Ed25519PrivateKey:
    descriptor = -1
    try:
        descriptor = os.open(
            Path(path), os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC
        )
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid != os.geteuid()
            or stat.S_IMODE(before.st_mode) & 0o077
            or not 1 <= before.st_size <= 16 * 1024
        ):
            raise HostHelperAuthorityError("host helper private key is unsafe")
        raw = os.read(descriptor, 16 * 1024 + 1)
        after = os.fstat(descriptor)
        identity = lambda value: (
            value.st_dev,
            value.st_ino,
            value.st_size,
            value.st_mtime_ns,
            value.st_ctime_ns,
        )
        if len(raw) > 16 * 1024 or identity(before) != identity(after):
            raise HostHelperAuthorityError("host helper private key changed while read")
    except HostHelperAuthorityError:
        raise
    except OSError as error:
        raise HostHelperAuthorityError(
            "host helper private key is unavailable"
        ) from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    try:
        key = serialization.load_pem_private_key(raw, password=None)
    except (TypeError, ValueError) as error:
        raise HostHelperAuthorityError("host helper private key is invalid") from error
    if not isinstance(key, ed25519.Ed25519PrivateKey):
        raise HostHelperAuthorityError("host helper private key must be Ed25519")
    return key


class HostHelperGrantIssuer:
    """Sign one short-lived, exact host operation for one GPU node."""

    def __init__(
        self,
        private_key: ed25519.Ed25519PrivateKey,
        *,
        clock: Callable[[], datetime] | None = None,
        request_id_factory: Callable[[], object] | None = None,
    ) -> None:
        if not isinstance(private_key, ed25519.Ed25519PrivateKey):
            raise TypeError("host helper authority key must be Ed25519")
        if clock is not None and not callable(clock):
            raise TypeError("host helper authority clock is invalid")
        if request_id_factory is not None and not callable(request_id_factory):
            raise TypeError("host helper request ID factory is invalid")
        self._private_key = private_key
        self._clock = clock or (lambda: datetime.now(UTC))
        self._request_id_factory = request_id_factory or uuid4
        self.public_key = private_key.public_key()
        self.public_key_bytes = self.public_key.public_bytes_raw()
        self.key_id = hashlib.sha256(self.public_key_bytes).hexdigest()

    @classmethod
    def from_private_key_file(
        cls,
        path: Path,
        *,
        clock: Callable[[], datetime] | None = None,
        request_id_factory: Callable[[], object] | None = None,
    ) -> HostHelperGrantIssuer:
        return cls(
            _load_private_key(Path(path)),
            clock=clock,
            request_id_factory=request_id_factory,
        )

    def public_key_document(self) -> dict[str, object]:
        return {
            "algorithm": "ed25519",
            "authority": HOST_HELPER_AUTHORITY,
            "key_id": self.key_id,
            "public_key": self.public_key_bytes.hex(),
            "schema_version": 1,
            "usage": "host-maintenance-grant",
        }

    def issue_grant(
        self,
        *,
        node_id: object,
        operation: object,
        expires_in_seconds: object,
        request_id: object | None = None,
    ) -> SignedHostHelperGrant:
        if not isinstance(
            operation,
            (
                ExecuteContainerRuntimeRequestOperation,
                InstallVonkDebOperation,
                ConfirmPackageActivationOperation,
            ),
        ):
            raise HostHelperAuthorityError("host helper operation is invalid")
        if (
            not isinstance(expires_in_seconds, int)
            or isinstance(expires_in_seconds, bool)
            or not 1 <= expires_in_seconds <= MAX_HOST_HELPER_GRANT_SECONDS
        ):
            raise HostHelperAuthorityError("host helper grant expiry is invalid")
        now = self._now()
        if not isinstance(node_id, str):
            raise HostHelperAuthorityError("host helper grant binding is invalid")
        try:
            claims = HostHelperGrantClaims(
                schema_version=1,
                authority=HOST_HELPER_AUTHORITY,
                request_id=str(
                    self._request_id_factory() if request_id is None else request_id
                ),
                node_id=node_id,
                issued_at=now,
                expires_at=now + expires_in_seconds,
                operation=operation,
            )
        except (AgentProtocolError, TypeError, ValueError) as error:
            raise HostHelperAuthorityError(
                "host helper grant binding is invalid"
            ) from error
        return SignedHostHelperGrant(
            schema_version=1,
            claims=claims,
            signature=HostHelperSignature(
                algorithm="ed25519",
                key_id=self.key_id,
                value=self._private_key.sign(
                    host_helper_grant_signing_bytes(claims)
                ).hex(),
            ),
        )

    def _now(self) -> int:
        try:
            now = self._clock()
        except Exception as error:
            raise HostHelperAuthorityError(
                "host helper authority clock is unavailable"
            ) from error
        if (
            not isinstance(now, datetime)
            or now.tzinfo is None
            or now.utcoffset() is None
        ):
            raise HostHelperAuthorityError(
                "host helper authority clock must be timezone-aware"
            )
        return int(now.astimezone(UTC).timestamp())


class HostRuntimeAuthorityService:
    """Bind a narrow host-runtime grant to one live agent attempt."""

    _ACTION_KINDS: ClassVar[dict[ContainerRuntimeAction, frozenset[str]]] = {
        ContainerRuntimeAction.RUNTIME_PREFLIGHT: frozenset({"runtime.preflight.v1"}),
        # A registry pull of a pinned runtime image from the Controller's
        # layered image store, as part of distributing it to a Spark.
        ContainerRuntimeAction.IMAGE_PULL: frozenset(
            {
                AgentOperation.ARTIFACT_DISTRIBUTION.value,
                AgentOperation.RECIPE_INSTALL.value,
                AgentOperation.RECIPE_START.value,
                AgentOperation.RECIPE_JOB_RUN.value,
            }
        ),
        ContainerRuntimeAction.IMAGE_INSPECT: frozenset({"recipe.install"}),
        ContainerRuntimeAction.RUN_INSPECT: frozenset({"recipe.start"}),
        ContainerRuntimeAction.START: frozenset({"recipe.start", "recipe.job.run.v1"}),
        # A start attempt may stop its own managed run when readiness fails.
        ContainerRuntimeAction.STOP: frozenset(
            {"recipe.start", "recipe.stop", "recipe.job.run.v1"}
        ),
        ContainerRuntimeAction.INSTALLATION_CLEANUP: frozenset(
            {"recipe.uninstall", "recipe.reconcile"}
        ),
    }

    def __init__(
        self,
        sessions: sessionmaker[Session],
        issuer: HostHelperGrantIssuer,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not callable(sessions):
            raise TypeError("host runtime sessions are invalid")
        if not isinstance(issuer, HostHelperGrantIssuer):
            raise TypeError("host runtime grant issuer is invalid")
        if clock is not None and not callable(clock):
            raise TypeError("host runtime authority clock is invalid")
        self._sessions = sessions
        self._issuer = issuer
        self._clock = clock or (lambda: datetime.now(UTC))

    @property
    def public_key_document(self) -> dict[str, object]:
        return self._issuer.public_key_document()

    def issue_grant(
        self,
        *,
        node_id: str,
        fence: str,
        action: ContainerRuntimeAction,
        request_sha256: str,
        certificate_serial: str,
        start_plan_sha256: str | None = None,
        stop_plan_sha256: str | None = None,
        run_generation: int | None = None,
        runtime_run_id: str | None = None,
        runtime_target_id: str | None = None,
        runtime_installation_id: str | None = None,
        installation_id: str | None = None,
        reconciliation_identity: RecipeReconciliationIdentity | None = None,
        installation_intent_nonce: str | None = None,
        expires_in_seconds: int = 30,
    ) -> SignedHostHelperGrant:
        if type(action) is not ContainerRuntimeAction:
            raise HostHelperAuthorityError("container runtime action is invalid")
        lease_deadline, plan_binding, intent_ordinal = self._check_attempt(
            node_id=node_id,
            fence=fence,
            action=action,
            start_plan_sha256=start_plan_sha256,
            stop_plan_sha256=stop_plan_sha256,
            run_generation=run_generation,
            runtime_run_id=runtime_run_id,
            runtime_target_id=runtime_target_id,
            runtime_installation_id=runtime_installation_id,
            installation_id=installation_id,
            reconciliation_identity=reconciliation_identity,
            request_sha256=request_sha256,
            certificate_serial=certificate_serial,
        )
        grant = self._issuer.issue_grant(
            node_id=node_id,
            operation=ExecuteContainerRuntimeRequestOperation(
                type=HostOperationKind.EXECUTE_CONTAINER_RUNTIME_REQUEST.value,
                action=action.value,
                fence=fence,
                request_sha256=request_sha256,
                start_plan_sha256=plan_binding.start_plan_sha256,
                stop_plan_sha256=plan_binding.stop_plan_sha256,
                run_generation=plan_binding.run_generation,
                runtime_run_id=plan_binding.runtime_run_id,
                runtime_target_id=plan_binding.runtime_target_id,
                runtime_installation_id=plan_binding.runtime_installation_id,
                installation_id=installation_id,
                reconciliation_identity=reconciliation_identity,
                installation_intent_nonce=installation_intent_nonce,
                installation_intent_ordinal=intent_ordinal,
            ),
            expires_in_seconds=expires_in_seconds,
        )
        if grant.claims.expires_at > int(lease_deadline.timestamp()):
            raise HostHelperAuthorityError(
                "host runtime grant exceeds the active attempt lease"
            )
        return grant

    def issue_agent_upgrade_grant(
        self,
        *,
        node_id: str,
        fence: str,
        package_sha256: str,
        package_signature: str,
        certificate_serial: str,
        expires_in_seconds: int = 30,
    ) -> SignedHostHelperGrant:
        now = self._clock()
        with self._sessions.begin() as session:
            current = session.scalar(
                select(AgentOperationAttempt).where(
                    AgentOperationAttempt.fence == fence
                )
            )
            operation = (
                None
                if current is None
                else session.get(StoredAgentOperation, current.operation_id)
            )
            lease_deadline = (
                None
                if current is None
                else current.lease_deadline
                if current.lease_deadline.tzinfo is not None
                else current.lease_deadline.replace(tzinfo=UTC)
            )
            if (
                operation is None
                or current is None
                or operation.node_id != node_id
                or operation.kind != "agent.upgrade.v1"
                or operation.payload.get("package_sha256") != package_sha256
                or operation.payload.get("package_signature") != package_signature
                or operation.state != "running"
                or operation.current_attempt != current.attempt
                or current.state != "running"
                or current.agent_certificate_serial != certificate_serial
                or lease_deadline is None
                or lease_deadline <= now
            ):
                raise HostHelperAuthorityError("agent upgrade authority is stale")
            payload = read_stored_model(AgentUpgradePayload, operation.payload)
            if (
                payload.rollback.activation_deadline
                <= int(now.timestamp()) + expires_in_seconds
            ):
                raise HostHelperAuthorityError(
                    "package activation deadline has expired"
                )
        grant = self._issuer.issue_grant(
            node_id=node_id,
            operation=InstallVonkDebOperation(
                type=HostOperationKind.INSTALL_VONK_DEB.value,
                package_sha256=package_sha256,
                package_signature=package_signature,
                rollback=payload.rollback,
            ),
            expires_in_seconds=expires_in_seconds,
        )
        if grant.claims.expires_at > int(lease_deadline.timestamp()):
            raise HostHelperAuthorityError(
                "agent upgrade grant exceeds the active attempt lease"
            )
        return grant

    def issue_package_activation_grant(
        self,
        *,
        node_id: str,
        receipt: PackageActivationReceipt,
        runtime_identity: AgentRuntimeIdentity,
        certificate_serial: str,
    ) -> SignedHostHelperGrant:
        now = self._clock()
        with self._sessions() as session:
            node = session.get(AgentNode, node_id)
            certificate = session.get(AgentCertificate, certificate_serial)
            if (
                node is None
                or node.state != "active"
                or node.revoked_at is not None
                or certificate is None
                or certificate.node_id != node_id
                or certificate.revoked_at is not None
            ):
                raise HostHelperAuthorityError("activation node identity is invalid")
            operations = session.scalars(
                select(StoredAgentOperation).where(
                    StoredAgentOperation.node_id == node_id,
                    StoredAgentOperation.kind == "agent.upgrade.v1",
                    StoredAgentOperation.state.in_(
                        agent_operation_states.RUNNING_OR_PARKED
                    ),
                )
            )
            matching = []
            for operation in operations:
                payload = read_stored_model(AgentUpgradePayload, operation.payload)
                if matches_receipt(receipt, payload, node_id):
                    matching.append(payload)
            if len(matching) != 1:
                raise HostHelperAuthorityError(
                    "activation receipt has no unique upgrade authority"
                )
            payload = matching[0]
            if (
                receipt.phase != "armed"
                or payload.rollback.activation_deadline <= int(now.timestamp()) + 30
                or runtime_identity.binary_digest != payload.target_binary_digest
                or runtime_identity.build_digest != payload.target_build_digest
                or runtime_identity.architecture != payload.architecture
            ):
                raise HostHelperAuthorityError(
                    "candidate activation identity is invalid"
                )
        return self._issuer.issue_grant(
            node_id=node_id,
            operation=ConfirmPackageActivationOperation(
                type="confirm-package-activation",
                package_sha256=payload.package_sha256,
                attempt_nonce=payload.rollback.attempt_nonce,
            ),
            expires_in_seconds=30,
        )

    def _check_attempt(
        self,
        *,
        node_id: str,
        fence: str,
        action: ContainerRuntimeAction,
        start_plan_sha256: str | None,
        stop_plan_sha256: str | None,
        run_generation: int | None,
        runtime_run_id: str | None,
        runtime_target_id: str | None,
        runtime_installation_id: str | None,
        installation_id: str | None,
        reconciliation_identity: RecipeReconciliationIdentity | None,
        request_sha256: str,
        certificate_serial: str,
    ) -> tuple[datetime, RuntimePlanBinding, int | None]:
        now = self._clock()
        with self._sessions() as session:
            current = session.scalar(
                select(AgentOperationAttempt).where(
                    AgentOperationAttempt.fence == fence
                )
            )
            operation = (
                None
                if current is None
                else session.get(StoredAgentOperation, current.operation_id)
            )
            parent = (
                None if operation is None else session.get(Job, operation.parent_job_id)
            )
            node = session.get(AgentNode, node_id)
            certificate = session.get(AgentCertificate, certificate_serial)
            cancellation_requested = bool(
                parent is not None
                and isinstance(parent.result, Mapping)
                and parent.result.get("cancel_requested") is True
            )
            cancellation_stop = (
                cancellation_requested and action is ContainerRuntimeAction.STOP
            )
            cancellation_deadline = superseded_cancellation_deadline(
                parent.result if parent is not None else None
            )
            lease_deadline = (
                None
                if current is None
                else current.lease_deadline
                if current.lease_deadline.tzinfo is not None
                else current.lease_deadline.replace(tzinfo=UTC)
            )
            if (
                operation is None
                or current is None
                or parent is None
                or node is None
                or certificate is None
                or (
                    parent.state not in {"queued", "running"}
                    and not is_deferred_stop(parent, operation)
                )
                or node_id not in parent.targets
                or node.state != "active"
                or node.revoked_at is not None
                or certificate.node_id != node_id
                or certificate.state != "active"
                or certificate.revoked_at is not None
                or certificate.ca_revoked_at is not None
                or _aware(certificate.not_before) > _aware(now)
                or _aware(certificate.not_after) <= _aware(now)
                or operation.node_id != node_id
                or operation.authority_revision != parent.authority_revision
                or operation.kind not in self._ACTION_KINDS[action]
                or operation.state != "running"
                or operation.current_attempt != current.attempt
                or current.state != "running"
                or current.agent_certificate_serial != certificate_serial
                or lease_deadline is None
                or lease_deadline <= now
                or (cancellation_requested and not cancellation_stop)
                or (
                    cancellation_requested
                    and (
                        cancellation_deadline is None
                        or _aware(now) >= cancellation_deadline
                    )
                )
                or (
                    operation.kind in _WORKLOAD_INTENT_OPERATIONS
                    and (
                        type(operation.workload_intent_ordinal) is not int
                        or operation.workload_intent_ordinal < 1
                        or operation.workload_intent_ordinal
                        != parent.payload.get("workload_intent_ordinal")
                        or (
                            operation.workload_intent_ordinal
                            != node.workload_intent_ordinal
                            and not cancellation_stop
                        )
                    )
                )
                or (
                    operation.payload.get("phase") == "collective-readiness"
                    and action is not ContainerRuntimeAction.RUN_INSPECT
                    and not cancellation_stop
                )
            ):
                raise HostHelperAuthorityError(
                    "container runtime action authority is stale"
                )
            if action is ContainerRuntimeAction.INSTALLATION_CLEANUP:
                try:
                    payload_bytes = canonical_message(operation.payload)
                    if operation.kind == "recipe.reconcile":
                        payload = read_stored_model(
                            RecipeReconcilePayload, payload_bytes, from_json=True
                        )
                        if not isinstance(
                            reconciliation_identity, RecipeReconciliationIdentity
                        ) or canonical_message(reconciliation_identity) != (
                            canonical_message(payload)
                        ):
                            raise ValueError("reconciliation identity differs")
                        expected_request = HostRuntimeRequest(
                            action="installation-cleanup",
                            fence=fence,
                            arguments=[],
                            installation_id=installation_id,
                            reconciliation_identity=reconciliation_identity,
                        )
                        if (
                            hashlib.sha256(
                                canonical_message(expected_request)
                            ).hexdigest()
                            != request_sha256
                        ):
                            raise ValueError("reconciliation request differs")
                        authorized = {payload.installation_id}
                    else:
                        if reconciliation_identity is not None:
                            raise ValueError(
                                "ordinary uninstall has reconciliation identity"
                            )
                        authorized = {
                            read_stored_model(
                                RecipeUninstallPayload, payload_bytes, from_json=True
                            ).installation_id
                        }
                except (TypeError, ValueError) as error:
                    raise HostHelperAuthorityError(
                        "container runtime cleanup authority is invalid"
                    ) from error
                if installation_id is None or installation_id not in authorized:
                    raise HostHelperAuthorityError(
                        "container runtime cleanup installation is unauthorized"
                    )
            elif installation_id is not None or reconciliation_identity is not None:
                raise HostHelperAuthorityError(
                    "container runtime installation binding is invalid"
                )
            try:
                binding = derive_runtime_plan_binding(
                    session,
                    parent=parent,
                    operation=operation,
                    node_id=node_id,
                    action=action,
                    cancellation_requested=cancellation_requested,
                    now=_aware(now),
                )
            except (RuntimePlanAuthorityError, TypeError, ValueError) as error:
                raise HostHelperAuthorityError(
                    "container runtime lifecycle authority is invalid"
                ) from error
            if (
                start_plan_sha256 != binding.start_plan_sha256
                or stop_plan_sha256 != binding.stop_plan_sha256
                or run_generation != binding.run_generation
                or runtime_run_id != binding.runtime_run_id
                or runtime_target_id != binding.runtime_target_id
                or runtime_installation_id != binding.runtime_installation_id
            ):
                raise HostHelperAuthorityError(
                    "container runtime lifecycle binding differs from exact plan"
                )
            return (
                min(lease_deadline, cancellation_deadline)
                if cancellation_stop and cancellation_deadline is not None
                else lease_deadline,
                binding,
                node.workload_intent_ordinal
                if action
                in {
                    ContainerRuntimeAction.START,
                    ContainerRuntimeAction.INSTALLATION_CLEANUP,
                }
                else None,
            )
