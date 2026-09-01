"""Controller-only signer for the narrow GPU node host-maintenance helper."""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import ClassVar
from uuid import uuid4

from cryptography.hazmat.primitives.asymmetric import ed25519
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import AgentProtocolError, canonical_message
from vonk_agent_protocol.host_helper import (
    HOST_HELPER_AUTHORITY,
    MAX_HOST_HELPER_GRANT_SECONDS,
    ContainerRuntimeAction,
    HostHelperGrantClaims,
    HostHelperOperation,
    HostHelperSignature,
    HostOperationKind,
    SignedHostHelperGrant,
    SignedRecipeRunObservationReceipt,
    host_helper_grant_signing_bytes,
    recipe_run_observation_receipt_signing_bytes,
)

from .compat_recovery import (
    _GRANTLESS_RETRY_FAILURE as COMPAT_GRANTLESS_RETRY_FAILURE,
)
from .compat_recovery import (
    GRANTLESS_RETRY_CERTIFICATE_SERIAL as COMPAT_GRANTLESS_RETRY_CERTIFICATE_SERIAL,
)
from .compat_recovery import (
    GRANTLESS_RETRY_FENCE as COMPAT_GRANTLESS_RETRY_FENCE,
)
from .compat_recovery import JOB_ID as COMPAT_JOB_ID
from .compat_recovery import (
    NODE_ID as COMPAT_NODE_ID,
)
from .compat_recovery import (
    OPERATION_ID as COMPAT_OPERATION_ID,
)
from .compat_recovery import RECOVERY_ID as COMPAT_RECOVERY_ID
from .compat_recovery import RETRY_ATTEMPT as COMPAT_RETRY_ATTEMPT
from .compat_recovery import (
    SOURCE_BINARY_DIGEST as COMPAT_SOURCE_BINARY_DIGEST,
)
from .compat_recovery import (
    SOURCE_BUILD_DIGEST as COMPAT_SOURCE_BUILD_DIGEST,
)
from .compat_recovery import (
    SOURCE_SEMANTIC_VERSION as COMPAT_SOURCE_SEMANTIC_VERSION,
)
from .compat_recovery import (
    TARGET_BINARY_DIGEST as COMPAT_TARGET_BINARY_DIGEST,
)
from .compat_recovery import (
    TARGET_BUILD_DIGEST as COMPAT_TARGET_BUILD_DIGEST,
)
from .compat_recovery import (
    TARGET_PACKAGE_SHA256 as COMPAT_TARGET_PACKAGE_SHA256,
)
from .compat_recovery import (
    TARGET_PACKAGE_VERSION as COMPAT_TARGET_PACKAGE_VERSION,
)
from .models import (
    AgentCertificate,
    AgentNode,
    AgentOperationAttempt,
    AgentUpgradeCompatibilityRecovery,
    ClusterMapping,
    Job,
    LocalRecipeRevision,
    RecipeInstallation,
    RecipeRun,
    RecipeRunObservationGrant,
    RunNode,
)
from .models import AgentOperation as StoredAgentOperation
from .workload_helper_authority import _load_private_key

logger = logging.getLogger(__name__)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class HostHelperAuthorityError(RuntimeError):
    """The host-helper grant could not be issued safely."""


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
        if type(operation) is not HostHelperOperation:
            raise HostHelperAuthorityError("host helper operation is invalid")
        if (
            not isinstance(expires_in_seconds, int)
            or isinstance(expires_in_seconds, bool)
            or not 1 <= expires_in_seconds <= MAX_HOST_HELPER_GRANT_SECONDS
        ):
            raise HostHelperAuthorityError("host helper grant expiry is invalid")
        now = self._now()
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
        ContainerRuntimeAction.IMAGE_IMPORT: frozenset({"recipe.image.import.v1"}),
        ContainerRuntimeAction.IMAGE_INSPECT: frozenset({"recipe.install"}),
        ContainerRuntimeAction.RUN_INSPECT: frozenset({"recipe.start"}),
        ContainerRuntimeAction.START: frozenset({"recipe.start", "recipe.job.run.v1"}),
        # A start attempt may stop its own managed run when readiness fails.
        ContainerRuntimeAction.STOP: frozenset(
            {"recipe.start", "recipe.stop", "recipe.job.run.v1"}
        ),
    }
    # dev335 and the staged a122 recovery both request this exact TTL. Keep
    # equality below: this is a compatibility value, not a range or default.
    _COMPATIBILITY_GRANT_SECONDS: ClassVar[int] = 10
    _COMPATIBILITY_IDENTITY_WINDOW: ClassVar[timedelta] = timedelta(minutes=15)

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
        job_id: str,
        operation_id: str,
        attempt: int,
        fence: str,
        action: ContainerRuntimeAction,
        request_sha256: str,
        certificate_serial: str,
        expires_in_seconds: int = 30,
    ) -> SignedHostHelperGrant:
        if type(action) is not ContainerRuntimeAction:
            raise HostHelperAuthorityError("container runtime action is invalid")
        lease_deadline = self._check_attempt(
            node_id=node_id,
            job_id=job_id,
            operation_id=operation_id,
            attempt=attempt,
            fence=fence,
            action=action,
            certificate_serial=certificate_serial,
        )
        grant = self._issuer.issue_grant(
            node_id=node_id,
            operation=HostHelperOperation(
                HostOperationKind.EXECUTE_CONTAINER_RUNTIME_REQUEST,
                {
                    "action": action.value,
                    "job_id": job_id,
                    "operation_id": operation_id,
                    "attempt": attempt,
                    "fence": fence,
                    "request_sha256": request_sha256,
                },
            ),
            expires_in_seconds=expires_in_seconds,
        )
        if grant.claims.expires_at > int(lease_deadline.timestamp()):
            raise HostHelperAuthorityError(
                "host runtime grant exceeds the active attempt lease"
            )
        return grant

    def issue_recipe_run_observation_grant(
        self,
        *,
        node_id: str,
        certificate_serial: str,
        identity: Mapping[str, object],
        job_id: str,
        operation_id: str,
        attempt: int,
        fence: str,
        request_sha256: str,
        expires_in_seconds: int,
    ) -> tuple[str, SignedHostHelperGrant]:
        """Authorize one exact, read-only local rank inspection."""

        if expires_in_seconds != 10:
            raise HostHelperAuthorityError(
                "recipe run observation grant TTL is invalid"
            )
        now = _aware(self._clock())
        with self._sessions.begin() as session:
            observation_identity = self._validate_observation_identity(
                session,
                node_id=node_id,
                certificate_serial=certificate_serial,
                identity=identity,
                now=now,
            )
            if job_id != identity.get("run_id") or attempt != identity.get(
                "run_generation"
            ):
                raise HostHelperAuthorityError(
                    "recipe run observation execution binding is invalid"
                )
            run_node = session.scalar(
                select(RunNode)
                .where(
                    RunNode.run_id == identity["run_id"],
                    RunNode.node_id == node_id,
                )
                .with_for_update(of=RunNode)
            )
            assert run_node is not None
            pending = session.scalar(
                select(RecipeRunObservationGrant)
                .where(RecipeRunObservationGrant.run_node_id == run_node.id)
                .with_for_update()
            )
            if (
                pending is not None
                and pending.consumed is not True
                and pending.expires_at + 5 >= int(now.timestamp())
            ):
                raise HostHelperAuthorityError(
                    "recipe run observation grant is already pending"
                )
            grant = self._issuer.issue_grant(
                node_id=node_id,
                operation=HostHelperOperation(
                    HostOperationKind.EXECUTE_CONTAINER_RUNTIME_REQUEST,
                    {
                        "action": ContainerRuntimeAction.RUN_INSPECT.value,
                        "job_id": job_id,
                        "operation_id": operation_id,
                        "attempt": attempt,
                        "fence": fence,
                        "request_sha256": request_sha256,
                        "observation_identity_sha256": observation_identity,
                    },
                ),
                expires_in_seconds=expires_in_seconds,
            )
            if pending is None:
                pending = RecipeRunObservationGrant(run_node_id=run_node.id)
                session.add(pending)
            pending.request_id = grant.claims.request_id
            pending.identity_sha256 = observation_identity
            pending.issued_at = grant.claims.issued_at
            pending.expires_at = grant.claims.expires_at
            pending.consumed = False
            return observation_identity, grant

    def consume_recipe_run_observation_grant(
        self,
        session: Session,
        *,
        node_id: str,
        certificate_serial: str,
        identity: Mapping[str, object],
        observed_at: datetime,
        received_at: datetime,
        signed_grant: Mapping[str, object],
        helper_receipt: Mapping[str, object],
    ) -> tuple[str, bool, str]:
        """Verify and consume the exact grant echoed by an observation result."""

        now = _aware(received_at)
        observation_identity = self._validate_observation_identity(
            session,
            node_id=node_id,
            certificate_serial=certificate_serial,
            identity=identity,
            now=now,
        )
        try:
            grant = SignedHostHelperGrant.parse(signed_grant)
            self._issuer.public_key.verify(
                bytes.fromhex(grant.signature.value),
                host_helper_grant_signing_bytes(grant.claims),
            )
        except Exception as error:
            raise HostHelperAuthorityError(
                "recipe run observation grant signature is invalid"
            ) from error
        try:
            receipt = SignedRecipeRunObservationReceipt.parse(helper_receipt)
            node = session.get(AgentNode, node_id)
            if node is None or node.observation_receipt_public_key is None:
                raise HostHelperAuthorityError(
                    "recipe run observation receipt key is unavailable"
                )
            receipt_public_key = bytes.fromhex(node.observation_receipt_public_key)
            receipt_key_id = hashlib.sha256(receipt_public_key).hexdigest()
            if receipt.signature.key_id != receipt_key_id:
                raise HostHelperAuthorityError(
                    "recipe run observation receipt key is stale"
                )
            ed25519.Ed25519PublicKey.from_public_bytes(receipt_public_key).verify(
                bytes.fromhex(receipt.signature.value),
                recipe_run_observation_receipt_signing_bytes(receipt.claims),
            )
        except HostHelperAuthorityError:
            raise
        except Exception as error:
            raise HostHelperAuthorityError(
                "recipe run observation receipt signature is invalid"
            ) from error
        expected_operation = {
            "type": HostOperationKind.EXECUTE_CONTAINER_RUNTIME_REQUEST.value,
            "action": ContainerRuntimeAction.RUN_INSPECT.value,
            "job_id": identity["run_id"],
            "operation_id": grant.claims.operation.values.get("operation_id"),
            "attempt": identity["run_generation"],
            "fence": grant.claims.operation.values.get("fence"),
            "request_sha256": grant.claims.operation.values.get("request_sha256"),
            "observation_identity_sha256": observation_identity,
        }
        observed_epoch = int(_aware(observed_at).timestamp())
        if (
            grant.claims.node_id != node_id
            or grant.claims.operation.to_mapping() != expected_operation
            or not grant.signature.key_id == self._issuer.key_id
            or not grant.claims.issued_at <= observed_epoch <= grant.claims.expires_at
            or int(now.timestamp()) > grant.claims.expires_at + 5
            or receipt.claims.node_id != node_id
            or receipt.claims.request_id != grant.claims.request_id
            or receipt.claims.request_sha256
            != grant.claims.operation.values.get("request_sha256")
            or receipt.claims.observation_identity_sha256 != observation_identity
            or receipt.claims.observed_at != observed_epoch
            or not grant.claims.issued_at
            <= receipt.claims.observed_at
            <= grant.claims.expires_at
        ):
            raise HostHelperAuthorityError("recipe run observation grant is stale")
        run_node = session.scalar(
            select(RunNode).where(
                RunNode.run_id == identity["run_id"], RunNode.node_id == node_id
            )
        )
        assert run_node is not None
        pending = session.scalar(
            select(RecipeRunObservationGrant)
            .where(RecipeRunObservationGrant.run_node_id == run_node.id)
            .with_for_update()
        )
        if (
            pending is None
            or pending.request_id != grant.claims.request_id
            or pending.identity_sha256 != observation_identity
            or pending.consumed is not False
        ):
            raise HostHelperAuthorityError("recipe run observation grant was replayed")
        pending.consumed = True
        receipt_digest = hashlib.sha256(
            canonical_message(receipt.to_mapping())
        ).hexdigest()
        return observation_identity, receipt.claims.outcome == "running", receipt_digest

    @staticmethod
    def _validate_observation_identity(
        session: Session,
        *,
        node_id: str,
        certificate_serial: str,
        identity: Mapping[str, object],
        now: datetime,
    ) -> str:
        expected_fields = {
            "schema_version",
            "node_id",
            "run_id",
            "installation_id",
            "recipe_revision_id",
            "recipe_content_sha256",
            "mapping_id",
            "mapping_generation",
            "run_generation",
            "image_digest",
            "artifact_set_digest",
            "model_identity",
            "rank",
            "role",
            "world_size",
            "local_address",
            "master_address",
            "master_port",
            "port",
            "runtime_arguments_sha256",
        }
        if set(identity) != expected_fields or identity.get("schema_version") != 1:
            raise HostHelperAuthorityError("recipe run observation identity is invalid")
        run = session.get(RecipeRun, identity.get("run_id"))
        installation = session.get(RecipeInstallation, identity.get("installation_id"))
        revision = session.get(LocalRecipeRevision, identity.get("recipe_revision_id"))
        mapping = session.get(ClusterMapping, identity.get("mapping_id"))
        run_node = session.scalar(
            select(RunNode).where(
                RunNode.run_id == identity.get("run_id"),
                RunNode.node_id == node_id,
            )
        )
        node = session.get(AgentNode, node_id)
        certificate = session.get(AgentCertificate, certificate_serial)
        if (
            run is None
            or installation is None
            or revision is None
            or mapping is None
            or run_node is None
            or node is None
            or certificate is None
            or run.state != "running"
            or run_node.state != "running"
            or run.plan.get("observation_schema_version") != 2
            or installation.state != "installed"
            or revision.content_sha256 is None
            or mapping.state != "ready"
            or "recipe.run.inspect.exact.v1" not in set(node.capabilities or ())
            or certificate.node_id != node_id
            or certificate.state != "active"
            or certificate.revoked_at is not None
            or certificate.ca_revoked_at is not None
            or _aware(certificate.not_before) > now
            or _aware(certificate.not_after) <= now
        ):
            raise HostHelperAuthorityError("recipe run observation authority is stale")
        jobs = session.scalars(
            select(Job)
            .where(Job.kind == "recipe.start", Job.state == "succeeded")
            .order_by(Job.updated_at.desc(), Job.id.desc())
        )
        launch: Mapping[str, object] | None = None
        for job in jobs:
            if job.payload.get("owner_id") != run.id or not isinstance(
                job.result, Mapping
            ):
                continue
            evidence = job.result.get("launch_evidence")
            candidate = evidence.get(node_id) if isinstance(evidence, Mapping) else None
            if (
                isinstance(candidate, Mapping)
                and candidate.get("run_generation") == run.run_generation
            ):
                launch = candidate
                break
        if launch is None:
            raise HostHelperAuthorityError("recipe run launch evidence is unavailable")
        expected = {
            "schema_version": 1,
            "node_id": node_id,
            "run_id": run.id,
            "installation_id": installation.id,
            "recipe_revision_id": revision.id,
            "recipe_content_sha256": revision.content_sha256,
            "mapping_id": mapping.id,
            "mapping_generation": run.mapping_generation,
            "run_generation": run.run_generation,
            "image_digest": installation.image_digest.removeprefix("sha256:"),
            "artifact_set_digest": launch.get("artifact_set_digest"),
            "model_identity": launch.get("model_identity"),
            "rank": run_node.rank,
            "role": run_node.role,
            "world_size": launch.get("world_size"),
            "local_address": launch.get("local_address"),
            "master_address": launch.get("master_address"),
            "master_port": launch.get("master_port"),
            "port": run_node.port,
            "runtime_arguments_sha256": launch.get("runtime_arguments_sha256"),
        }
        if dict(identity) != expected:
            raise HostHelperAuthorityError("recipe run observation identity is stale")
        return hashlib.sha256(canonical_message(expected)).hexdigest()

    def issue_agent_upgrade_grant(
        self,
        *,
        node_id: str,
        job_id: str,
        operation_id: str,
        attempt: int,
        fence: str,
        package_sha256: str,
        package_signature: str,
        certificate_serial: str,
        expires_in_seconds: int = 30,
    ) -> SignedHostHelperGrant:
        now = self._clock()
        with self._sessions.begin() as session:
            operation = session.get(StoredAgentOperation, operation_id)
            current = session.scalar(
                select(AgentOperationAttempt).where(
                    AgentOperationAttempt.operation_id == operation_id,
                    AgentOperationAttempt.attempt == attempt,
                )
            )
            lease_deadline = (
                None
                if current is None
                else current.lease_deadline
                if current.lease_deadline.tzinfo is not None
                else current.lease_deadline.replace(tzinfo=UTC)
            )
            recovery = session.scalar(
                select(AgentUpgradeCompatibilityRecovery)
                .where(AgentUpgradeCompatibilityRecovery.operation_id == operation_id)
                .with_for_update(of=AgentUpgradeCompatibilityRecovery)
            )
            certificate_matches = bool(
                current is not None
                and (
                    current.agent_certificate_serial == certificate_serial
                    or (
                        recovery is not None
                        and recovery.id == COMPAT_RECOVERY_ID
                        and recovery.node_id == COMPAT_NODE_ID == node_id
                        and recovery.job_id == COMPAT_JOB_ID == job_id
                        and recovery.operation_id == COMPAT_OPERATION_ID == operation_id
                        and recovery.expected_retry_attempt
                        == COMPAT_RETRY_ATTEMPT
                        == attempt
                        and current.fence == fence
                        and current.fence == COMPAT_GRANTLESS_RETRY_FENCE
                        and current.agent_certificate_serial
                        == COMPAT_GRANTLESS_RETRY_CERTIFICATE_SERIAL
                        and current.agent_certificate_serial
                        == recovery.rearm_attempt_certificate_serial
                        and certificate_serial
                        == recovery.rearm_dispatch_certificate_serial
                    )
                )
            )
            if (
                operation is None
                or current is None
                or operation.node_id != node_id
                or operation.parent_job_id != job_id
                or operation.kind != "agent.upgrade.v1"
                or operation.payload.get("package_sha256") != package_sha256
                or operation.payload.get("package_signature") != package_signature
                or operation.state != "running"
                or operation.current_attempt != attempt
                or current.state != "running"
                or current.fence != fence
                or not certificate_matches
                or lease_deadline is None
                or lease_deadline <= now
            ):
                raise HostHelperAuthorityError("agent upgrade authority is stale")
            if recovery is not None:
                return self._issue_compatibility_recovery_grant(
                    session=session,
                    recovery=recovery,
                    operation=operation,
                    current=current,
                    lease_deadline=lease_deadline,
                    node_id=node_id,
                    job_id=job_id,
                    operation_id=operation_id,
                    attempt=attempt,
                    fence=fence,
                    package_sha256=package_sha256,
                    package_signature=package_signature,
                    certificate_serial=certificate_serial,
                    expires_in_seconds=expires_in_seconds,
                    now=now,
                )
        grant = self._issuer.issue_grant(
            node_id=node_id,
            operation=HostHelperOperation(
                HostOperationKind.INSTALL_VONK_DEB,
                {
                    "package_sha256": package_sha256,
                    "package_signature": package_signature,
                },
            ),
            expires_in_seconds=expires_in_seconds,
        )
        if grant.claims.expires_at > int(lease_deadline.timestamp()):
            raise HostHelperAuthorityError(
                "agent upgrade grant exceeds the active attempt lease"
            )
        return grant

    def _issue_compatibility_recovery_grant(
        self,
        *,
        session: Session,
        recovery: AgentUpgradeCompatibilityRecovery,
        operation: StoredAgentOperation,
        current: AgentOperationAttempt,
        lease_deadline: datetime,
        node_id: str,
        job_id: str,
        operation_id: str,
        attempt: int,
        fence: str,
        package_sha256: str,
        package_signature: str,
        certificate_serial: str,
        expires_in_seconds: int,
        now: datetime,
    ) -> SignedHostHelperGrant:
        """Issue or replay the sole scheduled-reboot grant; never fall through."""

        node = session.get(AgentNode, node_id)
        request_certificate = session.get(AgentCertificate, certificate_serial)
        source = session.scalar(
            select(AgentOperationAttempt).where(
                AgentOperationAttempt.operation_id == operation_id,
                AgentOperationAttempt.attempt == recovery.source_attempt,
            )
        )
        package = operation.payload
        exact = bool(
            recovery.node_id == COMPAT_NODE_ID == node_id
            and recovery.job_id == job_id
            and recovery.operation_id == COMPAT_OPERATION_ID == operation_id
            and recovery.authority_revision == operation.authority_revision
            and recovery.expected_retry_attempt == recovery.source_attempt + 1
            and recovery.expected_retry_attempt == attempt
            and current.attempt == attempt
            and current.fence == fence
            and (
                current.result != COMPAT_GRANTLESS_RETRY_FAILURE
                or current.fence == COMPAT_GRANTLESS_RETRY_FENCE
            )
            and (
                current.agent_certificate_serial == certificate_serial
                or (
                    current.agent_certificate_serial
                    == COMPAT_GRANTLESS_RETRY_CERTIFICATE_SERIAL
                    and current.result == COMPAT_GRANTLESS_RETRY_FAILURE
                )
            )
            and source is not None
            and source.attempt == recovery.source_attempt
            and source.fence == recovery.source_fence
            and source.agent_certificate_serial == recovery.source_certificate_serial
            and node is not None
            and node.contact_certificate_serial
            == recovery.rearm_dispatch_certificate_serial
            == certificate_serial
            and request_certificate is not None
            and request_certificate.serial == certificate_serial
            and request_certificate.node_id == node_id
            and request_certificate.state == "active"
            and request_certificate.revoked_at is None
            and request_certificate.ca_revoked_at is None
            and _aware(request_certificate.not_before) <= _aware(now)
            and _aware(request_certificate.not_after) > _aware(now)
            and node.semantic_version
            == recovery.source_semantic_version
            == COMPAT_SOURCE_SEMANTIC_VERSION
            and node.build_digest
            == recovery.source_build_digest
            == COMPAT_SOURCE_BUILD_DIGEST
            and node.binary_digest
            == recovery.source_binary_digest
            == COMPAT_SOURCE_BINARY_DIGEST
            and node.self_test_passed is True
            and recovery.package_sha256
            == package_sha256
            == package.get("package_sha256")
            == COMPAT_TARGET_PACKAGE_SHA256
            and package_signature == package.get("package_signature")
            and recovery.upgrade_payload_sha256
            == operation.payload_digest
            == hashlib.sha256(canonical_message(dict(package))).hexdigest()
            and recovery.target_package_version
            == package.get("package_version")
            == COMPAT_TARGET_PACKAGE_VERSION
            and recovery.target_binary_digest
            == package.get("target_binary_digest")
            == COMPAT_TARGET_BINARY_DIGEST
            and recovery.target_build_digest
            == package.get("target_build_digest")
            == COMPAT_TARGET_BUILD_DIGEST
            and recovery.rearm_attempt_certificate_serial
            == current.agent_certificate_serial
            and (
                current.result != COMPAT_GRANTLESS_RETRY_FAILURE
                or current.agent_certificate_serial
                == COMPAT_GRANTLESS_RETRY_CERTIFICATE_SERIAL
            )
            and recovery.rearm_dispatch_certificate_serial == certificate_serial
        )
        if not exact:
            self._log_compatibility_rejection(
                recovery, attempt=attempt, category="authority_mismatch"
            )
            raise HostHelperAuthorityError(
                "Spark3542 compatibility recovery authority is stale"
            )
        if expires_in_seconds != self._COMPATIBILITY_GRANT_SECONDS:
            self._log_compatibility_rejection(
                recovery, attempt=attempt, category="ttl_mismatch"
            )
            raise HostHelperAuthorityError(
                "Spark3542 compatibility recovery authority is stale"
            )
        if recovery.state == "issued":
            expires_at = recovery.grant_expires_at
            if (
                recovery.retry_fence != fence
                or recovery.retry_certificate_serial != certificate_serial
                or recovery.signed_grant is None
                or expires_at is None
                or (
                    expires_at
                    if expires_at.tzinfo is not None
                    else expires_at.replace(tzinfo=UTC)
                )
                <= now
            ):
                raise HostHelperAuthorityError(
                    "Spark3542 compatibility recovery grant is stale"
                )
            try:
                replay = SignedHostHelperGrant.parse(recovery.signed_grant)
            except (AgentProtocolError, TypeError, ValueError) as error:
                raise HostHelperAuthorityError(
                    "Spark3542 compatibility recovery grant is invalid"
                ) from error
            if (
                replay.claims.request_id != recovery.grant_request_id
                or replay.claims.node_id != COMPAT_NODE_ID
                or replay.claims.operation.to_mapping()
                != {"type": "schedule-reboot", "delay_seconds": 60}
            ):
                raise HostHelperAuthorityError(
                    "Spark3542 compatibility recovery grant is invalid"
                )
            return replay
        if recovery.state != "armed" or recovery.signed_grant is not None:
            self._log_compatibility_rejection(
                recovery, attempt=attempt, category="grant_unavailable"
            )
            raise HostHelperAuthorityError(
                "Spark3542 compatibility recovery grant is unavailable"
            )
        grant = self._issuer.issue_grant(
            node_id=node_id,
            operation=HostHelperOperation(
                HostOperationKind.SCHEDULE_REBOOT,
                {"delay_seconds": 60},
            ),
            expires_in_seconds=expires_in_seconds,
        )
        if grant.claims.expires_at > int(lease_deadline.timestamp()):
            raise HostHelperAuthorityError(
                "Spark3542 compatibility recovery grant exceeds the active attempt lease"
            )
        recovery.state = "issued"
        recovery.retry_fence = fence
        recovery.retry_certificate_serial = certificate_serial
        recovery.signed_grant = grant.to_mapping()
        recovery.grant_request_id = grant.claims.request_id
        recovery.grant_expires_at = datetime.fromtimestamp(
            grant.claims.expires_at, tz=UTC
        )
        recovery.identity_deadline = now + self._COMPATIBILITY_IDENTITY_WINDOW
        recovery.issued_at = now
        return grant

    @staticmethod
    def _log_compatibility_rejection(
        recovery: AgentUpgradeCompatibilityRecovery,
        *,
        attempt: int,
        category: str,
    ) -> None:
        logger.warning(
            "compatibility host-helper grant rejected",
            extra={
                "compatibility_recovery_id": recovery.id,
                "operation_id": recovery.operation_id,
                "attempt": attempt,
                "rejection_category": category,
            },
        )

    def _check_attempt(
        self,
        *,
        node_id: str,
        job_id: str,
        operation_id: str,
        attempt: int,
        fence: str,
        action: ContainerRuntimeAction,
        certificate_serial: str,
    ) -> datetime:
        now = self._clock()
        with self._sessions() as session:
            operation = session.get(StoredAgentOperation, operation_id)
            current = session.scalar(
                select(AgentOperationAttempt).where(
                    AgentOperationAttempt.operation_id == operation_id,
                    AgentOperationAttempt.attempt == attempt,
                )
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
                or operation.parent_job_id != job_id
                or operation.kind not in self._ACTION_KINDS[action]
                or operation.state != "running"
                or operation.current_attempt != attempt
                or current.state != "running"
                or current.fence != fence
                or current.agent_certificate_serial != certificate_serial
                or lease_deadline is None
                or lease_deadline <= now
                or (
                    operation.payload.get("phase") == "collective-readiness"
                    and action is not ContainerRuntimeAction.RUN_INSPECT
                )
            ):
                raise HostHelperAuthorityError(
                    "container runtime action authority is stale"
                )
            return lease_deadline
