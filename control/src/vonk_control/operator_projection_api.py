"""Singular operator API for Fleet, Model and Recipe projections.

The machine-facing agent transport is installed separately.  This module only
exposes authenticated operator reads and thin action adapters; action adapters
must call the existing Controller authorities and may not manufacture remote
state or log evidence.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Annotated, Any, Literal, Protocol

from fastapi import FastAPI, HTTPException, Path, Query, Request, status
from pydantic import ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .agent_api import AgentApiServices, EnrollmentGrantResponse
from .agent_upgrades import AgentUpgradeService
from .audit import AuditRecord
from .auth import MUTATION_ROLES, Actor
from .deployment_provenance_contract import DeploymentProvenance
from .enrollment_bootstrap import accepted_installer_url
from .fleet_projection import (
    FleetNode,
    FleetNodeIdentity,
    FleetSnapshot,
    TelemetryCapabilitiesResponse,
    TelemetryCurrentResponse,
    TelemetryHistoryResponse,
    TelemetryWorkloadsResponse,
)
from .library_projection import LibrarySelectorAmbiguous
from .models import Job, JobLogEntry
from .operation_api import bounded_error_responses
from .strict_json import StrictJSONModel
from .telemetry import TelemetryResolution

_NODE_PATTERN = r"^spk_[0-9a-f]{32}$"
_SELECTOR_PATTERN = r"^[^\x00-\x1f\x7f]{1,256}$"

FLEET_OPERATION_IDS = {
    ("get", "/api/fleet"): "getFleetStatus",
    ("get", "/api/fleet/{selector}"): "getFleetNode",
    ("get", "/api/fleet/{selector}/metrics/history"): "getFleetMetricsHistory",
    ("get", "/api/fleet/{selector}/metrics/current"): "getFleetMetricsCurrent",
    ("get", "/api/fleet/{selector}/metrics/capabilities"): "getFleetMetricsCapabilities",
    ("get", "/api/fleet/{selector}/metrics/workloads"): "getFleetMetricsWorkloads",
    ("get", "/api/fleet/{selector}/loginfo"): "getFleetLogInfo",
    ("post", "/api/fleet/{selector}/rename"): "renameFleetNode",
    ("post", "/api/fleet/enroll"): "enrollFleetNode",
    ("post", "/api/fleet/{selector}/re-enroll"): "reenrollFleetNode",
    ("post", "/api/fleet/{selector}/remove"): "removeFleetNode",
    ("post", "/api/fleet/upgrade"): "upgradeFleet",
}


class FleetRenameRequest(StrictJSONModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)

    display_name: str = Field(min_length=1, max_length=80, pattern=r"^[^\x00-\x1f\x7f]+$")


class FleetEnrollRequest(StrictJSONModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=80, pattern=r"^[^\x00-\x1f\x7f]+$")
    ttl_seconds: int = Field(default=900, ge=1, le=86_400)


class FleetUpgradeRequest(StrictJSONModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    selectors: list[str] | None = Field(default=None, min_length=1, max_length=64)
    all: bool = False
    strategy: Literal["one-at-a-time", "all-at-once"] = "one-at-a-time"


class FleetActionResponse(StrictJSONModel):
    schema_version: Literal[2] = 2
    action: Literal["enroll", "re-enroll", "remove", "upgrade"]
    state: str = Field(min_length=1, max_length=32)
    operation_id: str | None = Field(default=None, max_length=128)
    node_id: str | None = Field(default=None, pattern=_NODE_PATTERN)
    display_name: str | None = Field(default=None, max_length=80)
    plan_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    targets: list[str] = Field(default_factory=list, max_length=64)
    grant: EnrollmentGrantResponse | None = None
    provenance: DeploymentProvenance | None = None
    detail: str | None = Field(default=None, max_length=256)


class FleetLogEntry(StrictJSONModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    observed_at: datetime
    source: Literal["client", "monitor", "runtime", "job"]
    level: Literal["debug", "info", "warning", "error"]
    message: str = Field(min_length=1, max_length=4_096)
    evidence_id: str = Field(min_length=1, max_length=128)


class FleetLogResponse(StrictJSONModel):
    schema_version: Literal[2] = 2
    node_id: str = Field(pattern=_NODE_PATTERN)
    since: datetime | None
    lines: int = Field(ge=1, le=1_000)
    entries: list[FleetLogEntry] = Field(max_length=1_000)
    retained: bool
    follow: bool


class FleetLogProvider(Protocol):
    def list(
        self,
        node_id: str,
        *,
        since: datetime | None,
        lines: int,
        recipe: str | None,
        source: str | None,
        follow: bool,
    ) -> FleetLogResponse | Mapping[str, object]: ...


class FleetProvenanceProvider(Protocol):
    def snapshot(self) -> DeploymentProvenance: ...


class FleetEnrollmentProvider(Protocol):
    def create_named(self, *, name: str, ttl_seconds: int, actor: str, request_id: str) -> Mapping[str, object]: ...

    def create_reenrollment(self, node_id: str, actor: str, ttl_seconds: int, request_id: str) -> Mapping[str, object]: ...

    def revoke_node(self, node_id: str, actor: str) -> None: ...


class FleetUpgradeProvider(Protocol):
    def current_package(self) -> Mapping[str, object]: ...

    def preview(self, node_ids: Sequence[str], package: Mapping[str, object], *, strategy: str) -> Any: ...

    def apply(
        self,
        node_ids: Sequence[str],
        package: Mapping[str, object],
        *,
        plan_digest: str,
        actor: str,
        request_id: str,
        strategy: str,
    ) -> Any: ...


class FleetOperatorServices:
    def __init__(
        self,
        *,
        enrollment: FleetEnrollmentProvider | None = None,
        upgrades: FleetUpgradeProvider | None = None,
        logs: FleetLogProvider | None = None,
        provenance: FleetProvenanceProvider | None = None,
    ) -> None:
        self.enrollment = enrollment
        self.upgrades = upgrades
        self.logs = logs
        self.provenance = provenance


class FleetNodeDetailResponse(FleetNode):
    """Current Fleet node projection with supply-chain evidence attached."""

    provenance: DeploymentProvenance | None = None


class _AgentEnrollmentAdapter:
    """Expose the existing enrollment authority in the operator contract."""

    def __init__(self, services: AgentApiServices) -> None:
        self._services = services

    def _required(self) -> AgentApiServices:
        if self._services.enrollment is None:
            raise RuntimeError("agent enrollment is unavailable")
        if self._services.bootstrap is None:
            raise RuntimeError("agent enrollment bootstrap is unavailable")
        return self._services

    def _response(self, grant: Any) -> EnrollmentGrantResponse:
        services = self._required()
        bootstrap = services.bootstrap
        assert bootstrap is not None
        return EnrollmentGrantResponse(
            id=grant.id,
            expires_at=grant.expires_at.isoformat(),
            purpose=grant.purpose,
            token=grant.token,
            controller_endpoint=bootstrap.controller_endpoint,
            enrollment_endpoint=bootstrap.enrollment_endpoint,
            ca_fingerprint=bootstrap.ca_fingerprint,
            controller_address=bootstrap.controller_address,
            service_hostnames=list(bootstrap.service_hostnames),
            installer_url=accepted_installer_url(bootstrap.installer_url),
        )

    def create_named(
        self, *, name: str, ttl_seconds: int, actor: str, request_id: str
    ) -> Mapping[str, object]:
        del request_id
        services = self._required()
        assert services.enrollment is not None
        grant = services.enrollment.create(None, actor, ttl_seconds)
        return {
            "display_name": name,
            "state": "pending",
            "grant": self._response(grant).model_dump(mode="json"),
        }

    def create_reenrollment(
        self, node_id: str, actor: str, ttl_seconds: int, request_id: str
    ) -> Mapping[str, object]:
        del request_id
        services = self._required()
        assert services.enrollment is not None
        grant = services.enrollment.create_reenrollment(node_id, actor, ttl_seconds)
        return {
            "state": "pending",
            "grant": self._response(grant).model_dump(mode="json"),
        }

    def revoke_node(self, node_id: str, actor: str) -> None:
        services = self._required()
        assert services.enrollment is not None
        services.enrollment.revoke_node(node_id, actor)


class ControllerJobLogProvider:
    """Project retained, redacted Controller job evidence for one Spark."""

    def __init__(self, sessions: sessionmaker[Session], job_logs: Any) -> None:
        self._sessions = sessions
        self._job_logs = job_logs

    def list(
        self,
        node_id: str,
        *,
        since: datetime | None,
        lines: int,
        recipe: str | None,
        source: str | None,
        follow: bool,
    ) -> FleetLogResponse:
        if source is not None and source != "job":
            return FleetLogResponse(
                node_id=node_id,
                since=since,
                lines=lines,
                entries=[],
                retained=True,
                follow=False,
            )
        with self._sessions() as session:
            rows = list(
                session.execute(
                    select(Job, JobLogEntry)
                    .join(JobLogEntry, JobLogEntry.job_id == Job.id)
                    .order_by(JobLogEntry.created_at.desc(), JobLogEntry.digest.desc())
                    .limit(512)
                )
            )
        entries: list[FleetLogEntry] = []
        for job, log in rows:
            if node_id not in job.targets:
                continue
            if since is not None and log.created_at < since:
                continue
            if recipe is not None:
                payload = job.payload if isinstance(job.payload, Mapping) else {}
                recipe_value = payload.get("recipe") or payload.get("recipe_selector")
                if recipe_value != recipe:
                    continue
            content = self._job_logs.read(job.id, log.digest).decode(
                "utf-8", errors="replace"
            )
            for line in content.splitlines():
                if line:
                    entries.append(
                        FleetLogEntry(
                            observed_at=log.created_at,
                            source="job",
                            level="info",
                            message=line,
                            evidence_id=log.digest,
                        )
                    )
        entries.sort(key=lambda item: item.observed_at, reverse=True)
        return FleetLogResponse(
            node_id=node_id,
            since=since,
            lines=lines,
            entries=entries[:lines],
            retained=True,
            # DatabaseJobLogStore is retained evidence, not a live stream.
            follow=False,
        )


def build_fleet_operator_services(
    *,
    agent_services: AgentApiServices | None,
    upgrades: AgentUpgradeService | None,
    logs: FleetLogProvider | None = None,
    provenance: FleetProvenanceProvider | None = None,
    sessions: sessionmaker[Session] | None = None,
    job_logs: Any | None = None,
) -> FleetOperatorServices:
    """Build Fleet action adapters from existing Controller authorities.

    ``logs`` must return retained authenticated evidence when configured.  If
    ``sessions`` and the existing ``DatabaseJobLogStore`` are supplied, the
    helper builds the retained Controller evidence provider itself.  Neither
    path synthesizes remote log entries or falls back to SSH.
    """

    enrollment = (
        None if agent_services is None else _AgentEnrollmentAdapter(agent_services)
    )
    retained_logs = logs
    if retained_logs is None and sessions is not None and job_logs is not None:
        retained_logs = ControllerJobLogProvider(sessions, job_logs)
    return FleetOperatorServices(
        enrollment=enrollment,
        upgrades=upgrades,
        logs=retained_logs,
        provenance=provenance,
    )


def _node(snapshot: FleetSnapshot, selector: str) -> FleetNode:
    wanted = selector.casefold()
    matches = [
        value for value in snapshot.nodes
        if wanted in {value.id.casefold(), value.display_name.casefold(), value.hostname.casefold()}
    ]
    if not matches:
        raise KeyError(selector)
    if len(matches) > 1:
        raise LibrarySelectorAmbiguous(selector, [value.display_name for value in matches])
    return matches[0]


def _operator_error(error: Exception) -> HTTPException:
    if isinstance(error, LibrarySelectorAmbiguous):
        candidates = ", ".join(error.candidates[:16])
        return HTTPException(status_code=422, detail=f"selector is ambiguous: {error.selector}; candidates: {candidates}")
    if isinstance(error, KeyError):
        return HTTPException(status_code=404, detail="operator object not found")
    if isinstance(error, ValueError):
        return HTTPException(status_code=422, detail=str(error)[:256])
    return HTTPException(status_code=503, detail="operator projection unavailable")


def _deployment_provenance(
    provider: FleetProvenanceProvider | None,
) -> DeploymentProvenance | None:
    """Read configured deployment provenance, keeping absence distinct.

    ``None`` means the provenance feature is not configured. A configured
    provider that returns a document which no longer validates is corruption
    and must fail loudly instead of being reported as no provenance.
    """

    if provider is None:
        return None
    try:
        return DeploymentProvenance.model_validate(provider.snapshot())
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        raise _operator_error(error) from None


def _require_mutation(actor: Actor, method: str, route: str) -> None:
    """Use the shared role table for both cookie and bearer actors."""

    allowed = MUTATION_ROLES.get((method, route))
    if allowed is None or actor.role not in allowed:
        raise HTTPException(status_code=403, detail="insufficient role")


def install_operator_projection_routes(
    app: FastAPI,
    *,
    actor_dependency: Any,
    fleet_projection: Any | None,
    library_projection: Any | None,
    fleet_services: FleetOperatorServices | None = None,
    audits: Any | None = None,
) -> None:
    """Install the singular operator route hierarchy.

    ``fleet_services`` is deliberately an adapter boundary.  The parent wires
    it to enrollment, signed upgrades and retained authenticated evidence; this
    module never falls back to SSH or synthetic values.
    """

    from .library_api import install_library_routes
    from .operation_api import _ADMIN_OPERATION_IDS

    _ADMIN_OPERATION_IDS.update(FLEET_OPERATION_IDS)
    install_library_routes(
        app, actor_dependency=actor_dependency, projection=library_projection
    )
    authenticated = actor_dependency

    def fleet() -> Any:
        if fleet_projection is None:
            raise HTTPException(status_code=503, detail="fleet projection unavailable")
        return fleet_projection

    def audit(request: Request, actor: Actor, action: str, targets: tuple[str, ...]) -> None:
        if audits is not None:
            audits.append(
                AuditRecord(
                    request.state.request_id,
                    actor.subject,
                    action,
                    None,
                    targets,
                )
            )

    def provenance() -> DeploymentProvenance | None:
        return _deployment_provenance(
            None if fleet_services is None else fleet_services.provenance
        )

    def snapshot() -> FleetSnapshot:
        try:
            return fleet().read()
        except HTTPException:
            raise
        except (KeyError, OSError, RuntimeError, TypeError, ValueError) as error:
            raise _operator_error(error) from None

    def selected(selector: str) -> FleetNode:
        try:
            return _node(snapshot(), selector)
        except (KeyError, OSError, RuntimeError, TypeError, ValueError) as error:
            raise _operator_error(error) from None

    @app.get(
        "/api/fleet",
        response_model=FleetSnapshot,
        responses=bounded_error_responses(401, 503),
        operation_id="getFleetStatus",
    )
    def fleet_status(_actor: Actor = authenticated) -> FleetSnapshot:
        return snapshot()

    @app.get(
        "/api/fleet/{selector}/metrics/history",
        response_model=TelemetryHistoryResponse,
        responses=bounded_error_responses(401, 404, 422, 503),
        operation_id="getFleetMetricsHistory",
    )
    def fleet_metrics_history(
        selector: Annotated[str, Path(pattern=_SELECTOR_PATTERN)],
        start: Annotated[datetime, Query()],
        end: Annotated[datetime, Query()],
        resolution: Annotated[TelemetryResolution, Query()],
        maximum_points: Annotated[int, Query(ge=1, le=3_000)] = 1_500,
        key: Annotated[str | None, Query(min_length=1, max_length=96)] = None,
        device_id: Annotated[str | None, Query(min_length=1, max_length=128)] = None,
        interface_name: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
        run_id: Annotated[str | None, Query(min_length=1, max_length=128)] = None,
        _actor: Actor = authenticated,
    ) -> TelemetryHistoryResponse:
        node = _node(snapshot(), selector)
        try:
            return fleet().telemetry_history(
                node.id, start=start, end=end, resolution=resolution,
                maximum_points=maximum_points, key=key, device_id=device_id,
                interface_name=interface_name, run_id=run_id,
            )
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            raise _operator_error(error) from None

    @app.get(
        "/api/fleet/{selector}/metrics/current",
        response_model=TelemetryCurrentResponse,
        responses=bounded_error_responses(401, 404, 503),
        operation_id="getFleetMetricsCurrent",
    )
    def fleet_metrics_current(
        selector: Annotated[str, Path(pattern=_SELECTOR_PATTERN)],
        key: Annotated[str | None, Query(min_length=1, max_length=96)] = None,
        device_id: Annotated[str | None, Query(min_length=1, max_length=128)] = None,
        interface_name: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
        run_id: Annotated[str | None, Query(min_length=1, max_length=128)] = None,
        _actor: Actor = authenticated,
    ) -> TelemetryCurrentResponse:
        node = _node(snapshot(), selector)
        try:
            return fleet().telemetry_current(
                node.id, key=key, device_id=device_id,
                interface_name=interface_name, run_id=run_id,
            )
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            raise _operator_error(error) from None

    @app.get(
        "/api/fleet/{selector}/metrics/capabilities",
        response_model=TelemetryCapabilitiesResponse,
        responses=bounded_error_responses(401, 404, 503),
        operation_id="getFleetMetricsCapabilities",
    )
    def fleet_metrics_capabilities(
        selector: Annotated[str, Path(pattern=_SELECTOR_PATTERN)],
        _actor: Actor = authenticated,
    ) -> TelemetryCapabilitiesResponse:
        node = _node(snapshot(), selector)
        try:
            return fleet().telemetry_capabilities(node.id)
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            raise _operator_error(error) from None

    @app.get(
        "/api/fleet/{selector}/metrics/workloads",
        response_model=TelemetryWorkloadsResponse,
        responses=bounded_error_responses(401, 404, 422, 503),
        operation_id="getFleetMetricsWorkloads",
    )
    def fleet_metrics_workloads(
        selector: Annotated[str, Path(pattern=_SELECTOR_PATTERN)],
        run_id: Annotated[str | None, Query(min_length=1, max_length=128)] = None,
        state: Annotated[str | None, Query(min_length=1, max_length=32)] = None,
        _actor: Actor = authenticated,
    ) -> TelemetryWorkloadsResponse:
        node = _node(snapshot(), selector)
        try:
            return fleet().telemetry_workloads(node.id, run_id=run_id, state=state)
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            raise _operator_error(error) from None

    @app.get(
        "/api/fleet/{selector}/loginfo",
        response_model=FleetLogResponse,
        responses=bounded_error_responses(401, 404, 422, 503),
        operation_id="getFleetLogInfo",
    )
    def fleet_loginfo(
        selector: Annotated[str, Path(pattern=_SELECTOR_PATTERN)],
        since: Annotated[datetime | None, Query()] = None,
        lines: Annotated[int, Query(ge=1, le=1_000)] = 100,
        recipe: Annotated[str | None, Query(max_length=256)] = None,
        source: Annotated[Literal["client", "monitor", "runtime", "job"] | None, Query()] = None,
        follow: Annotated[bool, Query()] = False,
        _actor: Actor = authenticated,
    ) -> FleetLogResponse:
        node = _node(snapshot(), selector)
        if fleet_services is None or fleet_services.logs is None:
            raise HTTPException(status_code=503, detail="fleet log evidence unavailable")
        try:
            value = fleet_services.logs.list(
                node.id, since=since, lines=lines, recipe=recipe,
                source=source, follow=follow,
            )
            return FleetLogResponse.model_validate(value)
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            raise _operator_error(error) from None

    @app.get(
        "/api/fleet/{selector}",
        response_model=FleetNodeDetailResponse,
        responses=bounded_error_responses(401, 404, 422, 503),
        operation_id="getFleetNode",
    )
    def fleet_detail(
        selector: Annotated[str, Path(pattern=_SELECTOR_PATTERN)],
        _actor: Actor = authenticated,
    ) -> FleetNodeDetailResponse:
        node = _node(snapshot(), selector)
        return FleetNodeDetailResponse.model_validate(
            node.model_dump(mode="json") | {"provenance": provenance()}
        )

    @app.post(
        "/api/fleet/{selector}/rename",
        response_model=FleetNodeIdentity,
        responses=bounded_error_responses(401, 403, 404, 422, 503),
        operation_id="renameFleetNode",
    )
    def fleet_rename(
        selector: Annotated[str, Path(pattern=_SELECTOR_PATTERN)],
        body: FleetRenameRequest,
        request: Request,
        actor: Actor = authenticated,
    ) -> FleetNodeIdentity:
        _require_mutation(actor, "POST", "/api/fleet/{selector}/rename")
        node = _node(snapshot(), selector)
        try:
            result = fleet().update_display_name(node.id, body.display_name)
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            raise _operator_error(error) from None
        audit(request, actor, "fleet.node.rename", (node.id,))
        return result

    @app.post(
        "/api/fleet/enroll",
        response_model=FleetActionResponse,
        status_code=status.HTTP_201_CREATED,
        responses=bounded_error_responses(401, 403, 422, 503),
        operation_id="enrollFleetNode",
    )
    def fleet_enroll(
        body: FleetEnrollRequest,
        request: Request,
        actor: Actor = authenticated,
    ) -> FleetActionResponse:
        _require_mutation(actor, "POST", "/api/fleet/enroll")
        if fleet_services is None or fleet_services.enrollment is None:
            raise HTTPException(status_code=503, detail="fleet enrollment unavailable")
        try:
            value = fleet_services.enrollment.create_named(
                name=body.name, ttl_seconds=body.ttl_seconds,
                actor=actor.subject, request_id=request.state.request_id,
            )
            result = FleetActionResponse.model_validate({"action": "enroll", **value})
            audit(request, actor, "fleet.node.enroll", (body.name,))
            return result
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            raise _operator_error(error) from None

    @app.post(
        "/api/fleet/{selector}/re-enroll",
        openapi_extra={"x-vonk-request-body": "none"},
        response_model=FleetActionResponse,
        responses=bounded_error_responses(401, 403, 404, 422, 503),
        operation_id="reenrollFleetNode",
    )
    def fleet_reenroll(
        selector: Annotated[str, Path(pattern=_SELECTOR_PATTERN)],
        request: Request,
        actor: Actor = authenticated,
    ) -> FleetActionResponse:
        _require_mutation(actor, "POST", "/api/fleet/{selector}/re-enroll")
        node = _node(snapshot(), selector)
        if fleet_services is None or fleet_services.enrollment is None:
            raise HTTPException(status_code=503, detail="fleet enrollment unavailable")
        try:
            value = fleet_services.enrollment.create_reenrollment(
                node.id, actor.subject, 900, request.state.request_id
            )
            result = FleetActionResponse.model_validate(
                {"action": "re-enroll", "node_id": node.id, **value}
            )
            audit(request, actor, "fleet.node.re-enroll", (node.id,))
            return result
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            raise _operator_error(error) from None

    @app.post(
        "/api/fleet/{selector}/remove",
        openapi_extra={"x-vonk-request-body": "none"},
        response_model=FleetActionResponse,
        responses=bounded_error_responses(401, 403, 404, 503),
        operation_id="removeFleetNode",
    )
    def fleet_remove(
        selector: Annotated[str, Path(pattern=_SELECTOR_PATTERN)],
        request: Request,
        actor: Actor = authenticated,
    ) -> FleetActionResponse:
        _require_mutation(actor, "POST", "/api/fleet/{selector}/remove")
        node = _node(snapshot(), selector)
        if fleet_services is None or fleet_services.enrollment is None:
            raise HTTPException(status_code=503, detail="fleet removal unavailable")
        try:
            fleet_services.enrollment.revoke_node(node.id, actor.subject)
            result = FleetActionResponse(action="remove", state="accepted", node_id=node.id)
            audit(request, actor, "fleet.node.remove", (node.id,))
            return result
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            raise _operator_error(error) from None

    @app.post(
        "/api/fleet/upgrade",
        response_model=FleetActionResponse,
        status_code=status.HTTP_202_ACCEPTED,
        responses=bounded_error_responses(401, 403, 404, 409, 422, 503),
        operation_id="upgradeFleet",
    )
    def fleet_upgrade(
        body: FleetUpgradeRequest,
        request: Request,
        actor: Actor = authenticated,
    ) -> FleetActionResponse:
        _require_mutation(actor, "POST", "/api/fleet/upgrade")
        if fleet_services is None or fleet_services.upgrades is None:
            raise HTTPException(status_code=503, detail="fleet upgrades unavailable")
        if body.all == (body.selectors is not None):
            raise HTTPException(status_code=422, detail="choose all or selectors")
        fleet_snapshot = snapshot()
        try:
            nodes = list(fleet_snapshot.nodes) if body.all else [_node(fleet_snapshot, value) for value in body.selectors or []]
            node_ids = list(dict.fromkeys(node.id for node in nodes))
            package = fleet_services.upgrades.current_package()
            plan = fleet_services.upgrades.preview(node_ids, package, strategy=body.strategy)
            job = fleet_services.upgrades.apply(
                node_ids, package, plan_digest=plan.plan_digest,
                actor=actor.subject, request_id=request.state.request_id,
                strategy=body.strategy,
            )
            result = FleetActionResponse(
                action="upgrade", state=str(getattr(job, "state", "accepted")),
                operation_id=str(getattr(job, "id", "")) or None,
                plan_digest=plan.plan_digest,
                targets=node_ids,
                provenance=provenance(),
            )
            audit(request, actor, "fleet.upgrade", tuple(node_ids))
            return result
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            raise _operator_error(error) from None


__all__ = [
    "FleetActionResponse",
    "FleetEnrollRequest",
    "FleetEnrollmentProvider",
    "FleetLogEntry",
    "FleetLogProvider",
    "FleetLogResponse",
    "FleetNodeDetailResponse",
    "FleetOperatorServices",
    "FleetProvenanceProvider",
    "FleetRenameRequest",
    "FleetUpgradeProvider",
    "FleetUpgradeRequest",
    "build_fleet_operator_services",
    "install_operator_projection_routes",
]
