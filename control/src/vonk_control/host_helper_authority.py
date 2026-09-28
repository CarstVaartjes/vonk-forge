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
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import AgentProtocolError, canonical_message
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
    SignedRecipeRunObservationReceipt,
    host_helper_grant_signing_bytes,
    recipe_run_observation_receipt_signing_bytes,
)
from vonk_agent_protocol.package_upgrade import PackageActivationReceipt
from vonk_agent_protocol.recipe_observations import RecipeRunObservationIdentity
from vonk_agent_protocol.recipe_operations import (
    RecipeReconcilePayload,
    RecipeUninstallPayload,
)
from vonk_forge_contracts import read_recipe

from .agent_jobs import (
    _WORKLOAD_INTENT_OPERATIONS,
    superseded_cancellation_deadline,
)
from .distributed_recovery import (
    _accepted_start_authority,
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
    CatalogDocumentRevision,
    ClusterMapping,
    Job,
    RecipeInstallation,
    RecipeRun,
    RecipeRunObservationGrant,
    RunNode,
)
from .models import AgentOperation as StoredAgentOperation
from .package_activation import matches_receipt
from .recipe_execution_contract import (
    RecipeExecutionContractError,
    parse_stored_run_plan,
)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def recipe_run_known(session: Session, run_id: str) -> bool:
    """Whether this Controller has any record of one recipe run.

    One predicate owns "unowned": the read-only probe grant and the agent's
    disposition lookup must never disagree about a run this Controller knows.
    """

    return session.get(RecipeRun, run_id) is not None or (
        session.scalar(select(RunNode.id).where(RunNode.run_id == run_id).limit(1))
        is not None
    )


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


class RecipeRunObservationReplayError(HostHelperAuthorityError):
    """A valid observation repeats an already consumed current grant."""


class RecipeRunObservationPendingError(HostHelperAuthorityError):
    """An unconsumed observation grant for this run is still outstanding.

    This is the bounded, *expected* refusal: the Controller already authorized
    this exact run and the agent's previous grant has not been consumed yet.
    It must stay distinguishable from an authority rejection, because the agent
    retries a pending grant and must never read it as a lost authorization.
    """


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
        ContainerRuntimeAction.IMAGE_IMPORT: frozenset(
            {"recipe.image.import.v1", "artifact.distribution.v1"}
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
        expires_in_seconds: int = 30,
    ) -> SignedHostHelperGrant:
        if type(action) is not ContainerRuntimeAction:
            raise HostHelperAuthorityError("container runtime action is invalid")
        lease_deadline, plan_binding = self._check_attempt(
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
                raise RecipeRunObservationPendingError(
                    "recipe run observation grant is already pending"
                )
            grant = self._issuer.issue_grant(
                node_id=node_id,
                operation=ExecuteContainerRuntimeRequestOperation(
                    type=HostOperationKind.EXECUTE_CONTAINER_RUNTIME_REQUEST.value,
                    action=ContainerRuntimeAction.RUN_INSPECT.value,
                    fence=fence,
                    request_sha256=request_sha256,
                    observation_identity_sha256=observation_identity,
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

    def issue_unowned_recipe_run_probe_grant(
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
    ) -> tuple[str, SignedHostHelperGrant] | None:
        """Authorize a read-only probe of a local run this Controller never owned.

        A Spark can retain the lifecycle of a run that no longer exists in this
        Controller's database, for example after the database was rebuilt.  No
        exact observation can ever be accepted for it, so the agent needs one
        authenticated answer instead: whether the process still runs.  The
        grant is the same exact, ten-second, node-bound ``RUN_INSPECT`` the
        helper already enforces; it changes nothing on the host, is never
        consumed as an observation of an owned run, and is refused whenever
        this Controller has any record of the run.  ``None`` means the run is
        known and the ordinary observation authority decides.
        """

        if expires_in_seconds != 10:
            raise HostHelperAuthorityError(
                "recipe run observation grant TTL is invalid"
            )
        now = _aware(self._clock())
        with self._sessions.begin() as session:
            try:
                normalized = RecipeRunObservationIdentity.model_validate(
                    dict(identity)
                ).model_dump(mode="json")
            except ValidationError as error:
                raise HostHelperAuthorityError(
                    "recipe run observation identity is invalid"
                ) from error
            run_id = normalized.get("run_id")
            if not isinstance(run_id, str) or recipe_run_known(session, run_id):
                return None
            node = session.get(AgentNode, node_id)
            certificate = session.get(AgentCertificate, certificate_serial)
            if (
                normalized.get("node_id") != node_id
                or job_id != run_id
                or attempt != normalized.get("run_generation")
                or node is None
                or node.state != "active"
                or node.revoked_at is not None
                or certificate is None
                or certificate.node_id != node_id
                or certificate.state != "active"
                or certificate.revoked_at is not None
                or certificate.ca_revoked_at is not None
                or _aware(certificate.not_before) > now
                or _aware(certificate.not_after) <= now
            ):
                raise HostHelperAuthorityError(
                    "recipe run probe authority is unavailable"
                )
            observation_identity = hashlib.sha256(
                canonical_message(normalized)
            ).hexdigest()
            grant = self._issuer.issue_grant(
                node_id=node_id,
                operation=ExecuteContainerRuntimeRequestOperation(
                    type=HostOperationKind.EXECUTE_CONTAINER_RUNTIME_REQUEST.value,
                    action=ContainerRuntimeAction.RUN_INSPECT.value,
                    fence=fence,
                    request_sha256=request_sha256,
                    observation_identity_sha256=observation_identity,
                ),
                expires_in_seconds=expires_in_seconds,
            )
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
        signed_grant: SignedHostHelperGrant,
        helper_receipt: SignedRecipeRunObservationReceipt,
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
            grant = signed_grant
            self._issuer.public_key.verify(
                bytes.fromhex(grant.signature.value),
                host_helper_grant_signing_bytes(grant.claims),
            )
        except Exception as error:
            raise HostHelperAuthorityError(
                "recipe run observation grant signature is invalid"
            ) from error
        try:
            receipt = helper_receipt
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
        operation = grant.claims.operation
        if not isinstance(operation, ExecuteContainerRuntimeRequestOperation):
            raise HostHelperAuthorityError(
                "recipe run observation grant operation is invalid"
            )
        expected_operation = {
            "type": HostOperationKind.EXECUTE_CONTAINER_RUNTIME_REQUEST.value,
            "action": ContainerRuntimeAction.RUN_INSPECT.value,
            "fence": operation.fence,
            "request_sha256": operation.request_sha256,
            "observation_identity_sha256": observation_identity,
        }
        observed_epoch = int(_aware(observed_at).timestamp())
        if (
            grant.claims.node_id != node_id
            or operation.to_mapping() != expected_operation
            or not grant.signature.key_id == self._issuer.key_id
            or not grant.claims.issued_at <= observed_epoch <= grant.claims.expires_at
            or int(now.timestamp()) > grant.claims.expires_at + 5
            or receipt.claims.node_id != node_id
            or receipt.claims.request_id != grant.claims.request_id
            or receipt.claims.request_sha256 != operation.request_sha256
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
        ):
            raise HostHelperAuthorityError("recipe run observation grant was replayed")
        if pending.consumed is not False:
            raise RecipeRunObservationReplayError(
                "recipe run observation grant was replayed"
            )
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
        try:
            identity = RecipeRunObservationIdentity.model_validate(
                dict(identity)
            ).model_dump(mode="json")
        except ValidationError as error:
            raise HostHelperAuthorityError(
                "recipe run observation identity is invalid"
            ) from error
        run = session.get(RecipeRun, identity.get("run_id"))
        installation = session.get(RecipeInstallation, identity.get("installation_id"))
        revision = session.get(
            CatalogDocumentRevision, identity.get("recipe_revision_id")
        )
        mapping = session.get(ClusterMapping, identity.get("mapping_id"))
        run_node = session.scalar(
            select(RunNode).where(
                RunNode.run_id == identity.get("run_id"),
                RunNode.node_id == node_id,
            )
        )
        node = session.get(AgentNode, node_id)
        certificate = session.get(AgentCertificate, certificate_serial)
        try:
            exact_observations = (
                parse_stored_run_plan(run.plan).observation_schema_version == 2
                if run is not None
                else False
            )
        except RecipeExecutionContractError:
            exact_observations = False
        if (
            run is None
            or installation is None
            or revision is None
            or mapping is None
            or run_node is None
            or node is None
            or certificate is None
            or run.state != "running"
            or run_node.state not in {"running", "failed"}
            or (run_node.state == "failed" and run.route_state != "withdrawn")
            or not exact_observations
            or installation.state != "installed"
            or revision.kind != "recipe"
            or revision.schema_version != 2
            or revision.state != "active"
            or mapping.state != "ready"
            or certificate.node_id != node_id
            or certificate.state != "active"
            or certificate.revoked_at is not None
            or certificate.ca_revoked_at is not None
            or _aware(certificate.not_before) > now
            or _aware(certificate.not_after) <= now
        ):
            raise HostHelperAuthorityError("recipe run observation authority is stale")
        try:
            read_recipe(revision.document)
        except (TypeError, ValueError) as error:
            raise HostHelperAuthorityError(
                "recipe run observation authority is stale"
            ) from error
        run_node_ids = tuple(
            session.scalars(
                select(RunNode.node_id)
                .where(RunNode.run_id == run.id)
                .order_by(RunNode.node_id)
            )
        )
        try:
            start, workload_intent_ordinal = _accepted_start_authority(
                session,
                run,
                revision.content_digest,
                node_id,
                allow_multi_target=len(run_node_ids) > 1,
                now=now,
            )
        except RuntimeError as error:
            raise HostHelperAuthorityError(
                "recipe run launch evidence is unavailable"
            ) from error
        if node.workload_intent_ordinal != workload_intent_ordinal or (
            isinstance(start.result, Mapping)
            and start.result.get("cancel_requested") is True
        ):
            raise HostHelperAuthorityError("recipe run launch evidence is unavailable")
        result = start.result
        evidence = (
            result.get("launch_evidence") if isinstance(result, Mapping) else None
        )
        launch = evidence.get(node_id) if isinstance(evidence, Mapping) else None
        if (
            not isinstance(launch, Mapping)
            or launch.get("run_generation") != run.run_generation
        ):
            raise HostHelperAuthorityError("recipe run launch evidence is unavailable")
        expected = RecipeRunObservationIdentity.model_validate(
            {
                "schema_version": 1,
                "node_id": node_id,
                "run_id": run.id,
                "installation_id": installation.id,
                "recipe_revision_id": revision.id,
                "recipe_content_sha256": revision.content_digest,
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
        ).model_dump(mode="json")
        if dict(identity) != expected:
            raise HostHelperAuthorityError("recipe run observation identity is stale")
        return hashlib.sha256(canonical_message(expected)).hexdigest()

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
            payload = AgentUpgradePayload.model_validate(operation.payload)
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
                    StoredAgentOperation.state.in_({"running", "waiting-for-operator"}),
                )
            )
            matching = []
            for operation in operations:
                payload = AgentUpgradePayload.model_validate(operation.payload)
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
    ) -> tuple[datetime, RuntimePlanBinding]:
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
                or parent.state not in {"queued", "running"}
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
                        payload = RecipeReconcilePayload.model_validate_json(
                            payload_bytes
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
                            RecipeUninstallPayload.model_validate_json(
                                payload_bytes
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
            )
