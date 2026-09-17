from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError
from vonk_control.agent_api import AgentApiServices, EnrollmentGrantResponse
from vonk_control.agent_upgrades import AgentUpgradeConflict
from vonk_control.auth import MUTATION_ROLES, Actor, CursorError
from vonk_control.deployment_provenance_contract import (
    DeploymentProvenance,
    PlatformObservation,
)
from vonk_control.enrollment import EnrollmentDenied, RemoteRevocationUncertain
from vonk_control.library_api import _error as library_error
from vonk_control.operator_projection_api import (
    FleetNodeDetailResponse,
    FleetOperatorServices,
    _AgentEnrollmentAdapter,
    _deployment_provenance,
    _operator_error,
    build_fleet_operator_services,
    install_operator_projection_routes,
)
from vonk_control.request_fault import RequestFault

_NODE = "spk_" + "a" * 32
_GRANT = {
    "id": "grant-1",
    "expires_at": "2026-09-10T12:00:00+00:00",
    "purpose": "new-node",
    "token": "t" * 43,
    "controller_endpoint": "https://controller.example.test",
    "enrollment_endpoint": "https://controller.example.test/agent/enroll",
    "ca_fingerprint": "a" * 64,
    "controller_address": "controller.example.test",
    "service_hostnames": ["controller.example.test"],
    "installer_url": "https://install.vonkforge.ai/dev/spark",
}


class _Enrollment:
    def create_named(self, **kwargs: object) -> dict[str, object]:
        assert kwargs["name"] == "Friendly Spark"
        return {"display_name": "Friendly Spark", "state": "pending", "grant": _GRANT}

    def create_reenrollment(self, *args: object, **kwargs: object) -> dict[str, object]:
        raise AssertionError("not used")

    def revoke_node(self, node_id: str, actor: str) -> None:
        raise AssertionError("not used")


def _app(actor: Actor, *, services: FleetOperatorServices | None = None) -> FastAPI:
    app = FastAPI()

    @app.middleware("http")
    async def request_id(request, call_next):
        request.state.request_id = "request-1"
        return await call_next(request)

    install_operator_projection_routes(
        app,
        actor_dependency=Depends(lambda: actor),
        fleet_projection=None,
        library_projection=None,
        fleet_services=services,
    )
    return app


def test_operator_routes_use_singular_namespaces_and_shared_mutation_roles() -> None:
    original = dict(MUTATION_ROLES)
    MUTATION_ROLES.update(
        {
            ("POST", "/api/fleet/enroll"): frozenset({"administrator"}),
            ("POST", "/api/fleet/{selector}/rename"): frozenset(
                {"operator", "administrator"}
            ),
            ("POST", "/api/fleet/upgrade"): frozenset({"administrator"}),
        }
    )
    try:
        app = _app(Actor("viewer", "viewer"))
        client = TestClient(app)
        assert (
            client.post("/api/fleet/enroll", json={"name": "Spark"}).status_code == 403
        )
        assert (
            client.post(
                f"/api/fleet/{_NODE}/rename", json={"display_name": "Spark"}
            ).status_code
            == 403
        )
        assert client.post("/api/fleet/upgrade", json={"all": True}).status_code == 403
        paths = set(app.openapi()["paths"])
        assert "/api/model/library" in paths
        assert "/api/model/{selector}" in paths
        assert "/api/recipe/library" in paths
        assert "/api/recipe/{selector}" in paths
        assert "/api/fleet" in paths
        assert not any(path.startswith("/api/v1/") for path in paths)
    finally:
        MUTATION_ROLES.clear()
        MUTATION_ROLES.update(original)


def test_enroll_preserves_canonical_one_time_bootstrap_material() -> None:
    original = dict(MUTATION_ROLES)
    MUTATION_ROLES[("POST", "/api/fleet/enroll")] = frozenset({"administrator"})
    try:
        app = _app(
            Actor("admin", "administrator"),
            services=FleetOperatorServices(enrollment=_Enrollment()),
        )
        payload = TestClient(app).post(
            "/api/fleet/enroll", json={"name": "Friendly Spark"}
        )
        assert payload.status_code == 201, payload.text
        result = payload.json()
        assert result["display_name"] == "Friendly Spark"
        assert result["grant"] == _GRANT
        assert EnrollmentGrantResponse.model_validate(result["grant"]).token == "t" * 43
    finally:
        MUTATION_ROLES.clear()
        MUTATION_ROLES.update(original)


def test_production_service_builder_does_not_enable_missing_authorities() -> None:
    services = build_fleet_operator_services(agent_services=None, upgrades=None)
    assert services.enrollment is None
    assert services.upgrades is None


def test_configured_provenance_document_that_no_longer_validates_fails_loudly() -> None:
    """An unconfigured provider is absent; a corrupt document is a fault.

    ``_deployment_provenance`` used to swallow the validation failure and the
    routes reported ``provenance: null`` for a stored observation that no
    longer satisfies its contract.
    """

    assert _deployment_provenance(None) is None

    class _CorruptProvenance:
        def snapshot(self) -> DeploymentProvenance:
            # The same ValidationError ``stored_observation`` raises for a
            # malformed stored observation.
            PlatformObservation.model_validate({})
            raise AssertionError("unreachable")

    with pytest.raises(HTTPException) as error:
        _deployment_provenance(_CorruptProvenance())
    # A corrupt stored document is the Controller's state, not a bad request,
    # and the detail names the failing field path without echoing its value.
    assert error.value.status_code == 503
    assert str(error.value.detail).startswith("stored document is invalid at ")


def test_corrupt_stored_observation_fails_the_fleet_node_detail() -> None:
    """``/api/fleet/{selector}`` must not report a corrupt observation as absent."""

    from vonk_control.fleet_projection import FleetSnapshot

    from .test_metrics import NODE, _fleet_snapshot

    class _Projection:
        def read(self) -> FleetSnapshot:
            return _fleet_snapshot()

    class _CorruptProvenance:
        def snapshot(self) -> DeploymentProvenance:
            # The same ValidationError ``stored_observation`` raises for a
            # malformed stored observation.
            PlatformObservation.model_validate({})
            raise AssertionError("unreachable")

    app = FastAPI()
    install_operator_projection_routes(
        app,
        actor_dependency=Depends(lambda: Actor("operator", "operator")),
        fleet_projection=_Projection(),
        library_projection=None,
        fleet_services=FleetOperatorServices(provenance=_CorruptProvenance()),
    )
    response = TestClient(app).get(f"/api/fleet/{NODE}")
    assert response.status_code == 503
    assert response.json()["detail"].startswith("stored document is invalid at ")


def test_fleet_node_detail_preserves_typed_live_observations() -> None:
    from vonk_control.fleet_projection import FleetSnapshot

    from .test_metrics import NODE, _fleet_snapshot

    snapshot = _fleet_snapshot()

    class _Projection:
        def read(self) -> FleetSnapshot:
            return snapshot

    app = FastAPI()
    install_operator_projection_routes(
        app,
        actor_dependency=Depends(lambda: Actor("operator", "operator")),
        fleet_projection=_Projection(),
        library_projection=None,
    )
    response = TestClient(app).get(f"/api/fleet/{NODE}")
    assert response.status_code == 200, response.text
    detail = FleetNodeDetailResponse.model_validate_json(response.content)
    assert detail.model_dump(exclude={"provenance"}) == snapshot.nodes[0].model_dump()


def test_only_an_explicit_request_fault_is_reported_as_the_callers_error() -> None:
    """A server-side failure must not answer 422, and a bad request must not 503.

    Both surfaces used to map any ``ValueError`` to 422, which also caught
    stored documents that no longer validate and the ORM's own integrity
    refusals.
    """

    try:
        PlatformObservation.model_validate({})
    except ValidationError as corrupt:
        stored = corrupt

    for mapper in (_operator_error, library_error):
        assert mapper(RequestFault("model library sort is invalid")).status_code == 422
        assert mapper(CursorError("model library cursor is invalid")).status_code == 422
        assert mapper(KeyError("missing")).status_code == 404
        # A stored document that no longer validates is the Controller's state.
        server = mapper(stored)
        assert server.status_code == 503
        assert str(server.detail).startswith("stored document is invalid at ")
        # So is an unexpected failure with no client-correctable cause.
        assert mapper(RuntimeError("projection unavailable")).status_code == 503


def test_agent_enrollment_adapter_binds_the_reviewed_display_name() -> None:
    """The adapter must not discard the name it was asked to enroll.

    ``_AgentEnrollmentAdapter.create_named`` used to call
    ``EnrollmentService.create(None, ...)``, so the operator-supplied name was
    never persisted as the grant's ``requested_display_name`` and the enrolling
    node fell back to its generated node id.
    """

    class _Grant:
        id = "grant-1"
        expires_at = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
        purpose = "new-node"
        token = "t" * 43

    class _EnrollmentService:
        def __init__(self) -> None:
            self.created: tuple[str, str, int] | None = None

        def create_named(self, name: str, actor: str, ttl_seconds: int) -> _Grant:
            self.created = (name, actor, ttl_seconds)
            return _Grant()

        def create(self, *_args: object) -> _Grant:
            raise AssertionError("the reviewed display name must not be dropped")

    enrollment = _EnrollmentService()
    services = SimpleNamespace(
        enrollment=enrollment,
        bootstrap=SimpleNamespace(
            controller_endpoint="https://controller.example.test",
            enrollment_endpoint="https://controller.example.test/agent/enroll",
            ca_fingerprint="a" * 64,
            controller_address="controller.example.test",
            service_hostnames=["controller.example.test"],
            installer_url="https://install.vonkforge.ai/dev/spark",
        ),
    )

    adapter = _AgentEnrollmentAdapter(cast(AgentApiServices, services))
    result = adapter.create_named(
        name="Living Spark", ttl_seconds=600, actor="admin", request_id="request-1"
    )

    assert enrollment.created == ("Living Spark", "admin", 600)
    assert result["display_name"] == "Living Spark"


def test_metrics_capabilities_forwards_the_telemetry_selectors() -> None:
    """The capabilities route must expose the selectors its siblings accept.

    ``telemetry_capabilities`` already accepted key, device_id, interface_name
    and run_id, and ``/metrics/current`` and ``/metrics/history`` declared them,
    but ``/metrics/capabilities`` declared only ``selector``, so the caller's
    scope was silently ignored.
    """

    from vonk_control.fleet_projection import (
        FleetSnapshot,
        TelemetryCapabilitiesResponse,
    )

    from .test_metrics import NODE, _fleet_snapshot

    class _Projection:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def read(self) -> FleetSnapshot:
            return _fleet_snapshot()

        def telemetry_capabilities(
            self, node_id: str, **selectors: object
        ) -> TelemetryCapabilitiesResponse:
            self.calls.append({"node_id": node_id, **selectors})
            return TelemetryCapabilitiesResponse(
                node_id=NODE,
                observed_at=datetime(2026, 8, 5, 12, tzinfo=UTC),
                received_at=datetime(2026, 8, 5, 12, tzinfo=UTC),
                freshness="live",
                capabilities=[],
            )

    projection = _Projection()
    app = FastAPI()
    install_operator_projection_routes(
        app,
        actor_dependency=Depends(lambda: Actor("operator", "operator")),
        fleet_projection=projection,
        library_projection=None,
    )
    response = TestClient(app).get(
        f"/api/fleet/{NODE}/metrics/capabilities",
        params={
            "key": "gpu.utilization",
            "device_id": "gpu0",
            "interface_name": "eth0",
            "run_id": "run-1",
        },
    )

    assert response.status_code == 200, response.text
    assert projection.calls == [
        {
            "node_id": NODE,
            "key": "gpu.utilization",
            "device_id": "gpu0",
            "interface_name": "eth0",
            "run_id": "run-1",
        }
    ]


class _SnapshotProjection:
    """Serve the shared typed Fleet snapshot to the action routes."""

    def read(self) -> object:
        from .test_metrics import _fleet_snapshot

        return _fleet_snapshot()


def _action_app(actor: Actor, *, services: FleetOperatorServices) -> FastAPI:
    app = FastAPI()

    @app.middleware("http")
    async def request_id(request, call_next):
        request.state.request_id = "request-1"
        return await call_next(request)

    install_operator_projection_routes(
        app,
        actor_dependency=Depends(lambda: actor),
        fleet_projection=_SnapshotProjection(),
        library_projection=None,
        fleet_services=services,
    )
    return app


class _RefusingUpgrade:
    """An upgrade authority that refuses before it previews or enqueues."""

    def __init__(self, error: Exception) -> None:
        self._error = error

    def current_package(self) -> dict[str, object]:
        raise self._error

    def preview(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("preview must not run once the authority refused")

    def apply(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("apply must not run once the authority refused")


@pytest.mark.parametrize(
    "reason",
    [
        "no outdated upgrade-capable Sparks were found",
        f"Spark {_NODE} is not currently online",
    ],
)
def test_a_refused_upgrade_names_the_authority_reason(reason: str) -> None:
    """Every specific upgrade refusal used to arrive as an unavailable projection.

    ``_operator_error`` had no branch for ``AgentUpgradeConflict``, so the
    refusal fell through to the generic 503 tail: the caller saw "operator
    projection unavailable" for a decision the upgrade domain had already
    explained, and blamed the projection layer for it.
    """

    app = _action_app(
        Actor("admin", "administrator"),
        services=FleetOperatorServices(
            upgrades=_RefusingUpgrade(AgentUpgradeConflict(reason))
        ),
    )
    response = TestClient(app).post("/api/fleet/upgrade", json={"all": True})

    assert response.status_code == 409, response.text
    assert response.headers["x-vonk-error-code"] == "controller.fleet.upgrade_conflict"
    assert response.json()["detail"] == reason


class _RefusingEnrollment:
    def __init__(self, error: Exception) -> None:
        self._error = error

    def create_named(self, **kwargs: object) -> dict[str, object]:
        raise AssertionError("not used")

    def create_reenrollment(self, *args: object, **kwargs: object) -> dict[str, object]:
        raise AssertionError("not used")

    def revoke_node(self, node_id: str, actor: str) -> None:
        raise self._error


@pytest.mark.parametrize(
    ("error", "status_code", "code"),
    [
        (
            EnrollmentDenied("node identity does not exist"),
            409,
            "controller.fleet.enrollment_denied",
        ),
        (
            RemoteRevocationUncertain(
                "local revocation complete; remote CA revocation is uncertain"
            ),
            503,
            "controller.fleet.revocation_uncertain",
        ),
    ],
)
def test_a_refused_removal_names_the_enrollment_layer(
    error: Exception, status_code: int, code: str
) -> None:
    """Removal shares the upgrade route's old blind spot.

    ``revoke_node`` refuses with ``EnrollmentDenied`` and reports a durable local
    revocation with a pending CA confirmation as ``RemoteRevocationUncertain``.
    Both are ``RuntimeError`` and used to reach the generic 503 tail, which
    blamed the projection for the enrollment authority's own decision.
    """

    from .test_metrics import NODE

    app = _action_app(
        Actor("admin", "administrator"),
        services=FleetOperatorServices(enrollment=_RefusingEnrollment(error)),
    )
    response = TestClient(app).post(f"/api/fleet/{NODE}/remove")

    assert response.status_code == status_code, response.text
    assert response.headers["x-vonk-error-code"] == code
    assert response.json()["detail"] == str(error)


class _RecordingEnrollment:
    def __init__(self) -> None:
        self.called = False

    def create_named(self, **kwargs: object) -> dict[str, object]:
        self.called = True
        # EnrollmentService caps a bootstrap grant at 900 seconds and voices the
        # refusal as this ValueError.
        raise ValueError("enrollment grant TTL must be between one and 900 seconds")

    def create_reenrollment(self, *args: object, **kwargs: object) -> dict[str, object]:
        raise AssertionError("not used")

    def revoke_node(self, node_id: str, actor: str) -> None:
        raise AssertionError("not used")


def test_enroll_rejects_a_ttl_the_bootstrap_authority_would_refuse() -> None:
    """The request model advertised a TTL the authority always refuses.

    ``ttl_seconds`` accepted up to 86400 while ``EnrollmentService`` caps a
    bootstrap grant at 900, so an overlong value passed request validation and
    the authority's ValueError arrived as "operator projection unavailable"
    instead of a request rejection.
    """

    enrollment = _RecordingEnrollment()
    app = _action_app(
        Actor("admin", "administrator"),
        services=FleetOperatorServices(enrollment=enrollment),
    )
    response = TestClient(app).post(
        "/api/fleet/enroll", json={"name": "Spark", "ttl_seconds": 901}
    )

    assert response.status_code == 422, response.text
    assert not enrollment.called
