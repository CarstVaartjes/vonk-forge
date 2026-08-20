"""Production route fencing for topology-safe GPU node update batches."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import uuid
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .agent_reconciliation import AgentReconciliationService
from .models import (
    AgentNode,
    Reconciliation,
    ReconciliationOperation,
    RoutePublication,
    RoutePublicationOwner,
)
from .route_runtime import (
    AcceptedEndpointEvidence,
    ActivationMarker,
    AtomicRouteBundlePublisher,
    RouteBundleRequest,
    RouteRuntimeError,
    endpoint_evidence_digest,
)

_NODE = re.compile(r"spk_[0-9a-f]{32}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_STATES = {"withdrawing", "withdrawn", "restored", "superseded"}
MAX_SAFE_RENEWAL_INTERVAL_SECONDS = 60
_RENEW_BEFORE_SECONDS = 90
_MIN_SAFE_REQUEST_HORIZON_SECONDS = 120
_RECORD_FIELDS = {
    "affected_aliases",
    "batch_index",
    "before_generation",
    "before_marker_digest",
    "key",
    "owner_generation",
    "owner_reconciliation_id",
    "restored_generation",
    "restored_marker_digest",
    "rollout_id",
    "schema_version",
    "state",
    "targets",
    "updated_at",
    "withdrawn_generation",
    "withdrawn_marker_digest",
}

type RouteRequestLoader = Callable[[Session, str], RouteBundleRequest]
type EndpointResolver = Callable[[Session, str], tuple[str, datetime]]


class UpdateRouteError(RuntimeError):
    """An update route transition could not be proven safe."""


@dataclass(frozen=True)
class RouteRenewalResult:
    """Closed result for scheduling one exact update route fence."""

    status: str
    receipt: str | None

    def __post_init__(self) -> None:
        if self.status not in {"renewed", "not-active", "withdrawal-pending"}:
            raise ValueError("route renewal result status is invalid")
        if self.status == "renewed":
            if not isinstance(self.receipt, str) or _DIGEST.fullmatch(self.receipt) is None:
                raise ValueError("renewed route receipt is invalid")
        elif self.receipt is not None:
            raise ValueError("inactive route renewal cannot carry a receipt")


@dataclass(frozen=True)
class RouteDrainReceipt:
    """Short-lived proof that one exact batch is absent from the live route set."""

    rollout_id: str
    batch_index: int
    targets: tuple[str, ...]
    route_digest: str
    drained_at: datetime
    expires_at: datetime
    evidence_digest: str

    def __post_init__(self) -> None:
        try:
            parsed = uuid.UUID(self.rollout_id)
        except (AttributeError, TypeError, ValueError) as error:
            raise ValueError("route drain rollout ID is invalid") from error
        drained = _aware(self.drained_at, "route drain timestamp")
        expires = _aware(self.expires_at, "route drain expiry")
        if (
            parsed.version != 4
            or str(parsed) != self.rollout_id
            or isinstance(self.batch_index, bool)
            or not isinstance(self.batch_index, int)
            or self.batch_index < 0
            or not self.targets
            or self.targets != tuple(sorted(set(self.targets)))
            or any(_NODE.fullmatch(node_id) is None for node_id in self.targets)
            or _DIGEST.fullmatch(self.route_digest) is None
            or not drained < expires <= drained + timedelta(seconds=300)
            or _DIGEST.fullmatch(self.evidence_digest) is None
            or self.evidence_digest != self._expected_digest()
        ):
            raise ValueError("route drain receipt is invalid")

    @classmethod
    def issue(
        cls,
        *,
        rollout_id: str,
        batch_index: int,
        targets: tuple[str, ...],
        route_digest: str,
        drained_at: datetime,
        expires_at: datetime,
    ) -> RouteDrainReceipt:
        exact_targets = tuple(sorted(targets))
        return cls(
            rollout_id=rollout_id,
            batch_index=batch_index,
            targets=exact_targets,
            route_digest=route_digest,
            drained_at=drained_at,
            expires_at=expires_at,
            evidence_digest=cls._digest_values(
                rollout_id=rollout_id,
                batch_index=batch_index,
                targets=exact_targets,
                route_digest=route_digest,
                drained_at=drained_at,
                expires_at=expires_at,
            ),
        )

    def _expected_digest(self) -> str:
        return self._digest_values(
            rollout_id=self.rollout_id,
            batch_index=self.batch_index,
            targets=self.targets,
            route_digest=self.route_digest,
            drained_at=self.drained_at,
            expires_at=self.expires_at,
        )

    @staticmethod
    def _digest_values(
        *,
        rollout_id: str,
        batch_index: int,
        targets: tuple[str, ...],
        route_digest: str,
        drained_at: datetime,
        expires_at: datetime,
    ) -> str:
        document = {
            "batch_index": batch_index,
            "drained_at": _aware(drained_at, "route drain timestamp").isoformat(),
            "expires_at": _aware(expires_at, "route drain expiry").isoformat(),
            "rollout_id": rollout_id,
            "route_digest": route_digest,
            "targets": list(targets),
        }
        return hashlib.sha256(_encoded(document)).hexdigest()


def _encoded(document: Mapping[str, object]) -> bytes:
    return (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _aware(value: datetime, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise UpdateRouteError(f"{label} must include a timezone")
    return value.astimezone(UTC)


@dataclass(frozen=True)
class _BoundaryRecord:
    schema_version: int
    key: str
    rollout_id: str
    batch_index: int
    targets: list[str]
    owner_reconciliation_id: str
    owner_generation: int
    before_marker_digest: str
    before_generation: int
    affected_aliases: list[str]
    state: str
    withdrawn_marker_digest: str | None
    withdrawn_generation: int | None
    restored_marker_digest: str | None
    restored_generation: int | None
    updated_at: str

    @property
    def content(self) -> bytes:
        return _encoded(asdict(self))


@dataclass(frozen=True)
class _PreparedOwner:
    reconciliation_id: str
    owner_generation: int
    plan_digest: str
    request: RouteBundleRequest
    marker: ActivationMarker | None = None


def load_authoritative_route_request(
    session: Session,
    reconciliation_id: str,
    *,
    endpoint_resolver: EndpointResolver,
    clock: Callable[[], datetime],
    lease_seconds: int = 180,
) -> RouteBundleRequest:
    """Rebuild routes only from accepted reconciliation and presence evidence."""

    if not 1 <= lease_seconds <= 300:
        raise UpdateRouteError("route publication lease is invalid")
    reconciliation = session.get(Reconciliation, reconciliation_id)
    if (
        reconciliation is None
        or reconciliation.status != "succeeded"
        or reconciliation.current_phase != "completed"
    ):
        raise UpdateRouteError("authoritative reconciliation is not completed")
    graph, document = AgentReconciliationService._validated_plan(reconciliation)
    nodes = list(
        session.scalars(
            select(AgentNode)
            .where(AgentNode.node_id.in_(graph.targets))
            .order_by(AgentNode.node_id)
        )
    )
    if [node.node_id for node in nodes] != list(graph.targets) or any(
        node.state != "active" or node.revoked_at is not None for node in nodes
    ):
        raise UpdateRouteError("authoritative reconciliation target is unavailable")
    publication = session.get(RoutePublication, reconciliation.id)
    if (
        publication is None
        or publication.state != "completed"
        or not isinstance(publication.evidence_digest, str)
        or _DIGEST.fullmatch(publication.evidence_digest) is None
    ):
        raise UpdateRouteError("authoritative route publication is incomplete")
    routes = document.get("routes")
    if not isinstance(routes, Mapping) or not routes:
        raise UpdateRouteError("authoritative reconciliation has no routes")
    projections = {
        row.graph_operation_id: row
        for row in session.scalars(
            select(ReconciliationOperation).where(
                ReconciliationOperation.reconciliation_id == reconciliation.id,
                ReconciliationOperation.role == "primary",
            )
        )
    }
    endpoints: dict[str, AcceptedEndpointEvidence] = {}
    for raw in routes.values():
        if not isinstance(raw, Mapping):
            raise UpdateRouteError("authoritative route is invalid")
        node_id = raw.get("entrypoint_node_id")
        workload_id = raw.get("workload_id")
        if not isinstance(node_id, str) or not isinstance(workload_id, str):
            raise UpdateRouteError("authoritative route entrypoint is invalid")
        operation_id = f"{workload_id}:{node_id}:workload.verify"
        projection = projections.get(operation_id)
        if (
            projection is None
            or projection.state != "accepted"
            or not isinstance(projection.evidence_digest, str)
            or _DIGEST.fullmatch(projection.evidence_digest) is None
        ):
            raise UpdateRouteError("authoritative route lacks accepted verify evidence")
        try:
            address, observed_at = endpoint_resolver(session, node_id)
        except Exception as error:
            raise UpdateRouteError("fresh route endpoint evidence is unavailable") from error
        observed = _aware(observed_at, "route endpoint observation")
        evidence_digest = endpoint_evidence_digest(
            node_id=node_id,
            address=address,
            observed_at=observed,
            operation_id=operation_id,
            verify_evidence_digest=projection.evidence_digest,
        )
        endpoints[node_id] = AcceptedEndpointEvidence(
            node_id=node_id,
            address=address,
            observed_at=observed,
            operation_id=operation_id,
            verify_evidence_digest=projection.evidence_digest,
            evidence_digest=evidence_digest,
        )
    plan_digest = reconciliation.plan_digest
    if not isinstance(plan_digest, str) or _DIGEST.fullmatch(plan_digest) is None:
        raise UpdateRouteError("authoritative reconciliation plan digest is invalid")
    now = _aware(clock(), "route publication clock")
    freshness_deadline = min(
        evidence.observed_at + timedelta(seconds=300)
        for evidence in endpoints.values()
    )
    expires_at = min(
        now + timedelta(seconds=lease_seconds),
        freshness_deadline,
    )
    if expires_at - now < timedelta(seconds=_MIN_SAFE_REQUEST_HORIZON_SECONDS):
        raise UpdateRouteError(
            "fresh route endpoint evidence cannot support safe renewal"
        )
    return RouteBundleRequest(
        reconciliation_id=reconciliation.id,
        plan_digest=plan_digest,
        evidence_set_digest=publication.evidence_digest,
        routes=routes,
        endpoints=endpoints,
        expires_at=expires_at,
        authority_revision=reconciliation.authority_revision,
    )


class ProductionUpdateRouteBoundary:
    """Fence and selectively withdraw routes around one GPU node update batch."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        publisher: AtomicRouteBundlePublisher,
        *,
        route_root: Path,
        request_loader: RouteRequestLoader,
        clock: Callable[[], datetime],
    ) -> None:
        if route_root.is_symlink() or not route_root.is_dir():
            raise UpdateRouteError("route runtime root is unavailable")
        records = route_root / "update-boundaries"
        if records.is_symlink():
            raise UpdateRouteError("route update record root is unsafe")
        records.mkdir(mode=0o700, exist_ok=True)
        records.chmod(0o700)
        self._sessions = sessions
        self._publisher = publisher
        self._records = records
        self._request_loader = request_loader
        self._clock = clock

    def withdraw(
        self,
        rollout_id: str,
        batch_index: int,
        targets: tuple[str, ...],
    ) -> RouteDrainReceipt:
        key, exact_targets = self._identity(rollout_id, batch_index, targets)
        record = self._read_record(key)
        if record is not None:
            self._require_identity(record, rollout_id, batch_index, exact_targets)
            if record.state == "superseded":
                raise UpdateRouteError("route update boundary was superseded")
        prepared = self._prepare_owner(record, require_marker=record is None)
        request = prepared.request
        if record is None:
            before = prepared.marker
            assert before is not None
            try:
                self._publisher.inspect(expected=before)
                self._publisher.claim_update_boundary(key)
            except RouteRuntimeError as error:
                raise UpdateRouteError(
                    "active route publication is not authoritative"
                ) from error
            affected = self._affected_aliases(request.routes, exact_targets)
            record = _BoundaryRecord(
                schema_version=1,
                key=key,
                rollout_id=rollout_id,
                batch_index=batch_index,
                targets=list(exact_targets),
                owner_reconciliation_id=prepared.reconciliation_id,
                owner_generation=prepared.owner_generation,
                before_marker_digest=before.digest,
                before_generation=before.generation,
                affected_aliases=list(affected),
                state="withdrawing",
                withdrawn_marker_digest=None,
                withdrawn_generation=None,
                restored_marker_digest=None,
                restored_generation=None,
                updated_at=self._timestamp(),
            )
            self._write_record(record)
            expected = before.digest
        elif record.state == "restored":
            current = self._publisher.inspect(verify_lease=False)
            if current.digest != record.restored_marker_digest:
                self._retain_superseded(record)
            affected = self._affected_aliases(request.routes, exact_targets)
            if list(affected) != record.affected_aliases:
                self._retain_superseded(record)
            self._publisher.claim_update_boundary(key)
            expected = current.digest
            record = replace(
                record,
                state="withdrawing",
                withdrawn_marker_digest=None,
                withdrawn_generation=None,
                restored_marker_digest=None,
                restored_generation=None,
                updated_at=self._timestamp(),
            )
            self._write_record(record)
        elif record.state == "withdrawn":
            current = self._publisher.inspect(verify_lease=False)
            if current.digest != record.withdrawn_marker_digest:
                self._retain_superseded(record)
            self._publisher.claim_update_boundary(key)
            expires = _aware(
                datetime.fromisoformat(current.expires_at),
                "route update boundary expiry",
            )
            if expires > _aware(self._clock(), "route update clock"):
                return self._drain_receipt(
                    rollout_id, batch_index, exact_targets, current
                )
            expected = current.digest
        elif record.state == "withdrawing":
            self._publisher.claim_update_boundary(key)
            expected = record.before_marker_digest
        else:
            raise UpdateRouteError("route update boundary state is invalid")
        marker = self._publish_withdrawal(
            record,
            request,
            exact_targets,
            renew=record.state == "withdrawn",
            expected_current_digest=expected,
        )
        if not self._owner_is_current(prepared):
            self._abort_and_retain(record, request, exact_targets, marker)
        withdrawn = replace(
            record,
            state="withdrawn",
            withdrawn_marker_digest=marker.digest,
            withdrawn_generation=marker.generation,
            updated_at=self._timestamp(),
        )
        self._write_record(withdrawn)
        return self._drain_receipt(
            rollout_id, batch_index, exact_targets, marker
        )

    def _drain_receipt(
        self,
        rollout_id: str,
        batch_index: int,
        targets: tuple[str, ...],
        marker: ActivationMarker,
    ) -> RouteDrainReceipt:
        drained_at = _aware(self._clock(), "route drain clock")
        expires_at = min(
            _aware(
                datetime.fromisoformat(marker.expires_at),
                "route drain marker expiry",
            ),
            drained_at + timedelta(seconds=300),
        )
        if expires_at <= drained_at:
            raise UpdateRouteError("route drain receipt has already expired")
        return RouteDrainReceipt.issue(
            rollout_id=rollout_id,
            batch_index=batch_index,
            targets=targets,
            route_digest=marker.digest,
            drained_at=drained_at,
            expires_at=expires_at,
        )

    def restore(
        self,
        rollout_id: str,
        batch_index: int,
        targets: tuple[str, ...],
    ) -> str:
        key, exact_targets = self._identity(rollout_id, batch_index, targets)
        record = self._read_record(key)
        if record is None:
            raise UpdateRouteError("route update withdrawal receipt is unavailable")
        self._require_identity(record, rollout_id, batch_index, exact_targets)
        if record.state == "superseded":
            raise UpdateRouteError("route update boundary was superseded")
        prepared = self._prepare_owner(record)
        current = self._publisher.inspect(verify_lease=False)
        if record.state == "restored":
            if current.digest != record.restored_marker_digest:
                self._retain_superseded(record)
            self._release_boundary(key)
            return current.digest
        if record.state != "withdrawn":
            raise UpdateRouteError("route update withdrawal is incomplete")
        if (
            current.digest != record.withdrawn_marker_digest
            and (
                current.reconciliation_id != prepared.reconciliation_id
                or current.plan_digest != prepared.plan_digest
            )
        ):
            self._retain_superseded(record)
        self._publisher.claim_update_boundary(key)
        try:
            restored_marker = self._publisher.publish(
                prepared.request,
                update_boundary_key=key,
                expected_current_digest=current.digest,
            )
        except RouteRuntimeError as error:
            raise UpdateRouteError(
                "authoritative route restoration failed"
            ) from error
        if not self._finalize_restored_marker(prepared, restored_marker):
            self._abort_and_retain(
                record,
                prepared.request,
                exact_targets,
                restored_marker,
            )
        restored = replace(
            record,
            state="restored",
            restored_marker_digest=restored_marker.digest,
            restored_generation=restored_marker.generation,
            updated_at=self._timestamp(),
        )
        self._write_record(restored)
        self._release_boundary(key)
        return restored_marker.digest

    def renew_active(
        self,
        rollout_id: str,
        batch_index: int,
        targets: tuple[str, ...],
    ) -> str:
        """Refresh one active partial bundle from current accepted evidence.

        Callers must invoke this at least every
        ``MAX_SAFE_RENEWAL_INTERVAL_SECONDS`` while the update owns routes.
        """

        key, exact_targets = self._identity(rollout_id, batch_index, targets)
        record = self._read_record(key)
        if record is None:
            raise UpdateRouteError("route update withdrawal receipt is unavailable")
        self._require_identity(record, rollout_id, batch_index, exact_targets)
        if record.state == "superseded":
            raise UpdateRouteError("route update boundary was superseded")
        if record.state != "withdrawn":
            raise UpdateRouteError("route update boundary is not renewable")
        prepared = self._prepare_owner(record)
        current = self._publisher.inspect(verify_lease=False)
        if (
            current.reconciliation_id != prepared.reconciliation_id
            or current.plan_digest != prepared.plan_digest
        ):
            self._retain_superseded(record)
        affected = self._affected_aliases(prepared.request.routes, exact_targets)
        if list(affected) != record.affected_aliases:
            self._retain_superseded(record)
        expected_state = (
            "maintenance"
            if len(record.affected_aliases) == len(prepared.request.routes)
            else "published"
        )
        if current.state != expected_state:
            self._retain_superseded(record)
        self._publisher.claim_update_boundary(key)
        expires = _aware(
            datetime.fromisoformat(current.expires_at),
            "route update boundary expiry",
        )
        remaining = expires - _aware(self._clock(), "route update clock")
        recovered = current.digest != record.withdrawn_marker_digest
        if recovered or remaining > timedelta(seconds=_RENEW_BEFORE_SECONDS):
            marker = current
        else:
            try:
                marker = self._publish_withdrawal(
                    record,
                    prepared.request,
                    exact_targets,
                    renew=True,
                    expected_current_digest=current.digest,
                )
            except UpdateRouteError as error:
                raise UpdateRouteError("route update boundary renewal failed") from error
        if not self._owner_is_current(prepared):
            self._abort_and_retain(
                record,
                prepared.request,
                exact_targets,
                marker,
            )
        renewed = replace(
            record,
            withdrawn_marker_digest=marker.digest,
            withdrawn_generation=marker.generation,
            updated_at=self._timestamp(),
        )
        self._write_record(renewed)
        return marker.digest

    def renew_if_active(
        self,
        rollout_id: str,
        batch_index: int,
        targets: tuple[str, ...],
    ) -> RouteRenewalResult:
        """Renew an exact fence or classify its benign inactive crash states."""

        key, exact_targets = self._identity(rollout_id, batch_index, targets)
        try:
            active_key = self._publisher.inspect_update_boundary()
        except RouteRuntimeError as error:
            raise UpdateRouteError("route update fence is unreadable") from error
        record = self._read_record(key)
        if record is None:
            if active_key is None:
                return RouteRenewalResult("not-active", None)
            if active_key == key:
                return RouteRenewalResult("withdrawal-pending", None)
            raise UpdateRouteError("route update fence belongs to a different rollout")
        self._require_identity(record, rollout_id, batch_index, exact_targets)
        if record.state == "superseded":
            raise UpdateRouteError("route update boundary was superseded")
        if active_key not in {None, key}:
            raise UpdateRouteError("route update fence belongs to a different rollout")
        if record.state == "restored":
            return RouteRenewalResult(
                "not-active" if active_key is None else "withdrawal-pending",
                None,
            )
        if record.state == "withdrawing":
            if active_key != key:
                raise UpdateRouteError("pending route withdrawal lost its exact fence")
            return RouteRenewalResult("withdrawal-pending", None)
        if record.state != "withdrawn":
            raise UpdateRouteError("route update boundary state is invalid")
        if active_key != key:
            raise UpdateRouteError("active route withdrawal lost its exact fence")
        return RouteRenewalResult(
            "renewed",
            self.renew_active(rollout_id, batch_index, exact_targets),
        )

    def _publish_withdrawal(
        self,
        record: _BoundaryRecord,
        request: RouteBundleRequest,
        targets: tuple[str, ...],
        *,
        renew: bool = False,
        expected_current_digest: str | None = None,
    ) -> ActivationMarker:
        unaffected = {
            alias: raw
            for alias, raw in request.routes.items()
            if alias not in set(record.affected_aliases)
        }
        try:
            if not unaffected:
                return self._publisher.withdraw(
                    reconciliation_id=request.reconciliation_id,
                    plan_digest=request.plan_digest,
                    targets=targets,
                    reason="GPU node platform update maintenance",
                    update_boundary_key=record.key,
                    renew_update_boundary=renew,
                    expected_current_digest=expected_current_digest,
                )
            entrypoints = {
                raw["entrypoint_node_id"]
                for raw in unaffected.values()
                if isinstance(raw, Mapping)
            }
            filtered = replace(
                request,
                routes=unaffected,
                endpoints={
                    node_id: evidence
                    for node_id, evidence in request.endpoints.items()
                    if node_id in entrypoints
                },
            )
            return self._publisher.publish(
                filtered,
                update_boundary_key=record.key,
                renew_update_boundary=renew,
                expected_current_digest=expected_current_digest,
            )
        except (KeyError, RouteRuntimeError) as error:
            raise UpdateRouteError("route withdrawal failed closed") from error

    def _load_request(
        self,
        session: Session,
        owner: RoutePublicationOwner,
    ) -> RouteBundleRequest:
        if owner.reconciliation_id is None:
            raise UpdateRouteError("route publication has no authoritative owner")
        try:
            request = self._request_loader(session, owner.reconciliation_id)
        except UpdateRouteError:
            raise
        except Exception as error:
            raise UpdateRouteError("authoritative route request is unavailable") from error
        if request.reconciliation_id != owner.reconciliation_id:
            raise UpdateRouteError("authoritative route request owner is invalid")
        if request.plan_digest != self._database_plan_digest(session, owner):
            raise UpdateRouteError("authoritative route request plan is stale")
        remaining = _aware(
            request.expires_at,
            "authoritative route request expiry",
        ) - _aware(self._clock(), "route update clock")
        if not (
            timedelta(seconds=_MIN_SAFE_REQUEST_HORIZON_SECONDS)
            <= remaining
            <= timedelta(seconds=300)
        ):
            raise UpdateRouteError(
                "authoritative route request lease cannot support safe renewal"
            )
        return request

    @staticmethod
    def _affected_aliases(
        routes: Mapping[str, object], targets: tuple[str, ...]
    ) -> tuple[str, ...]:
        target_set = set(targets)
        affected: list[str] = []
        for alias, raw in sorted(routes.items()):
            if not isinstance(alias, str) or not isinstance(raw, Mapping):
                raise UpdateRouteError("authoritative route fields are invalid")
            nodes = raw.get("nodes")
            entrypoint = raw.get("entrypoint_node_id")
            if (
                not isinstance(nodes, (list, tuple))
                or not nodes
                or any(not isinstance(node, str) for node in nodes)
                or not isinstance(entrypoint, str)
                or entrypoint not in nodes
            ):
                raise UpdateRouteError("authoritative route membership is invalid")
            if entrypoint in target_set or target_set.intersection(nodes):
                affected.append(alias)
        return tuple(affected)

    @staticmethod
    def _owner(session: Session) -> RoutePublicationOwner:
        owner = session.scalar(
            select(RoutePublicationOwner)
            .where(RoutePublicationOwner.singleton_id == 1)
            .with_for_update(of=RoutePublicationOwner)
        )
        if owner is None or owner.reconciliation_id is None:
            raise UpdateRouteError("route publication owner is unavailable")
        return owner

    def _prepare_owner(
        self,
        record: _BoundaryRecord | None,
        *,
        require_marker: bool = False,
    ) -> _PreparedOwner:
        drifted = False
        prepared: _PreparedOwner | None = None
        with self._sessions.begin() as session:
            owner = self._owner(session)
            if record is not None and not self._same_owner(record, owner):
                drifted = True
            else:
                request = self._load_request(session, owner)
                plan_digest = self._database_plan_digest(session, owner)
                assert owner.reconciliation_id is not None
                prepared = _PreparedOwner(
                    reconciliation_id=owner.reconciliation_id,
                    owner_generation=owner.owner_generation,
                    plan_digest=plan_digest,
                    request=request,
                    marker=(
                        self._database_marker(session, owner)
                        if require_marker
                        else None
                    ),
                )
        if drifted:
            assert record is not None
            self._retain_superseded(record)
        assert prepared is not None
        return prepared

    def _owner_is_current(self, prepared: _PreparedOwner) -> bool:
        with self._sessions.begin() as session:
            owner = self._owner(session)
            return (
                owner.reconciliation_id == prepared.reconciliation_id
                and owner.owner_generation == prepared.owner_generation
                and self._database_plan_digest(session, owner)
                == prepared.plan_digest
            )

    def _finalize_restored_marker(
        self,
        prepared: _PreparedOwner,
        marker: ActivationMarker,
    ) -> bool:
        with self._sessions.begin() as session:
            owner = self._owner(session)
            if (
                owner.reconciliation_id != prepared.reconciliation_id
                or owner.owner_generation != prepared.owner_generation
                or self._database_plan_digest(session, owner)
                != prepared.plan_digest
            ):
                return False
            publication = session.scalar(
                select(RoutePublication)
                .where(
                    RoutePublication.reconciliation_id
                    == prepared.reconciliation_id
                )
                .with_for_update(of=RoutePublication)
            )
            if publication is None or publication.state != "completed":
                return False
            publication.generation = marker.generation
            publication.plan_digest = marker.plan_digest
            publication.evidence_digest = marker.evidence_set_digest
            publication.route_digest = marker.routes_sha256
            publication.litellm_digest = marker.litellm_sha256
            publication.bundle_digest = marker.manifest_sha256
            publication.activation_marker = asdict(marker)
            publication.activation_marker_digest = marker.digest
            publication.lease_issued_at = datetime.fromisoformat(marker.issued_at)
            publication.lease_expires_at = datetime.fromisoformat(marker.expires_at)
            return True

    @staticmethod
    def _database_plan_digest(
        session: Session, owner: RoutePublicationOwner
    ) -> str:
        reconciliation = session.get(Reconciliation, owner.reconciliation_id)
        if (
            reconciliation is None
            or not isinstance(reconciliation.plan_digest, str)
            or _DIGEST.fullmatch(reconciliation.plan_digest) is None
        ):
            raise UpdateRouteError("route publication owner plan is unavailable")
        return reconciliation.plan_digest

    def _database_marker(
        self, session: Session, owner: RoutePublicationOwner
    ) -> ActivationMarker:
        publication = session.get(RoutePublication, owner.reconciliation_id)
        if (
            publication is None
            or publication.state != "completed"
            or not isinstance(publication.activation_marker, Mapping)
            or not isinstance(publication.activation_marker_digest, str)
        ):
            raise UpdateRouteError("authoritative route publication is incomplete")
        try:
            marker = ActivationMarker(**dict(publication.activation_marker))
        except TypeError as error:
            raise UpdateRouteError("authoritative route marker is invalid") from error
        if marker.digest != publication.activation_marker_digest:
            raise UpdateRouteError("authoritative route marker binding is invalid")
        return marker

    @staticmethod
    def _same_owner(
        record: _BoundaryRecord, owner: RoutePublicationOwner
    ) -> bool:
        return (
            owner.reconciliation_id == record.owner_reconciliation_id
            and owner.owner_generation == record.owner_generation
        )

    def _retain_superseded(self, record: _BoundaryRecord) -> None:
        superseded = replace(
            record,
            state="superseded",
            updated_at=self._timestamp(),
        )
        self._write_record(superseded)
        raise UpdateRouteError("route publication was superseded")

    def _abort_and_retain(
        self,
        record: _BoundaryRecord,
        request: RouteBundleRequest,
        targets: tuple[str, ...],
        current: ActivationMarker,
    ) -> None:
        try:
            self._publisher.withdraw(
                reconciliation_id=request.reconciliation_id,
                plan_digest=request.plan_digest,
                targets=targets,
                reason="update route authority changed during publication",
                update_boundary_key=record.key,
                expected_current_digest=current.digest,
            )
        except RouteRuntimeError:
            pass
        self._retain_superseded(record)

    def _release_boundary(self, key: str) -> None:
        try:
            self._publisher.release_update_boundary(key)
        except RouteRuntimeError as error:
            if "does not own publication" not in str(error):
                raise UpdateRouteError("route update boundary release failed") from error

    @staticmethod
    def _identity(
        rollout_id: str, batch_index: int, targets: tuple[str, ...]
    ) -> tuple[str, tuple[str, ...]]:
        try:
            parsed = uuid.UUID(rollout_id)
        except (AttributeError, TypeError, ValueError) as error:
            raise UpdateRouteError("update rollout ID is invalid") from error
        if str(parsed) != rollout_id:
            raise UpdateRouteError("update rollout ID is invalid")
        if isinstance(batch_index, bool) or not isinstance(batch_index, int) or batch_index < 0:
            raise UpdateRouteError("update batch index is invalid")
        if (
            not isinstance(targets, tuple)
            or not targets
            or len(targets) != len(set(targets))
            or any(not isinstance(target, str) or _NODE.fullmatch(target) is None for target in targets)
        ):
            raise UpdateRouteError("update route targets are invalid")
        exact_targets = tuple(sorted(targets))
        key = hashlib.sha256(
            _encoded(
                {
                    "batch_index": batch_index,
                    "rollout_id": rollout_id,
                    "schema_version": 1,
                    "targets": list(exact_targets),
                }
            )
        ).hexdigest()
        return key, exact_targets

    def _path(self, key: str) -> Path:
        return self._records / f"{key}.json"

    def _read_record(self, key: str) -> _BoundaryRecord | None:
        path = self._path(key)
        if not path.exists():
            return None
        if path.is_symlink() or not path.is_file() or self._records.is_symlink():
            raise UpdateRouteError("route update boundary record is unsafe")
        try:
            content = path.read_bytes()
            raw: Any = json.loads(content)
        except (OSError, json.JSONDecodeError) as error:
            raise UpdateRouteError("route update boundary record is unreadable") from error
        if (
            len(content) > 8192
            or not isinstance(raw, dict)
            or set(raw) != _RECORD_FIELDS
            or content != _encoded(raw)
        ):
            raise UpdateRouteError("route update boundary record is invalid")
        try:
            record = _BoundaryRecord(**raw)
        except TypeError as error:
            raise UpdateRouteError("route update boundary record is invalid") from error
        self._validate_record(record, key)
        return record

    @staticmethod
    def _validate_record(record: _BoundaryRecord, key: str) -> None:
        digest_values = (
            record.key,
            record.before_marker_digest,
            record.withdrawn_marker_digest,
            record.restored_marker_digest,
        )
        if (
            record.schema_version != 1
            or record.key != key
            or any(value is not None and _DIGEST.fullmatch(value) is None for value in digest_values)
            or record.state not in _STATES
            or isinstance(record.owner_generation, bool)
            or not isinstance(record.owner_generation, int)
            or record.owner_generation < 0
            or isinstance(record.before_generation, bool)
            or not isinstance(record.before_generation, int)
            or record.before_generation <= 0
            or not isinstance(record.targets, list)
            or record.targets != sorted(set(record.targets))
            or not isinstance(record.affected_aliases, list)
            or record.affected_aliases != sorted(set(record.affected_aliases))
            or any(not isinstance(alias, str) or not alias for alias in record.affected_aliases)
        ):
            raise UpdateRouteError("route update boundary record is invalid")
        _aware(datetime.fromisoformat(record.updated_at), "route update record timestamp")

    def _write_record(self, record: _BoundaryRecord) -> None:
        path = self._path(record.key)
        if path.is_symlink() or self._records.is_symlink():
            raise UpdateRouteError("route update boundary record is unsafe")
        descriptor, temporary_raw = tempfile.mkstemp(
            prefix=f".{path.name}-", dir=self._records
        )
        temporary = Path(temporary_raw)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as output:
                output.write(record.content)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
            directory = os.open(self._records, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError as error:
            raise UpdateRouteError("route update boundary record write failed") from error
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _require_identity(
        record: _BoundaryRecord,
        rollout_id: str,
        batch_index: int,
        targets: tuple[str, ...],
    ) -> None:
        if (
            record.rollout_id != rollout_id
            or record.batch_index != batch_index
            or record.targets != list(targets)
        ):
            raise UpdateRouteError("route update boundary identity conflicts")

    def _timestamp(self) -> str:
        return _aware(self._clock(), "route update clock").isoformat()
