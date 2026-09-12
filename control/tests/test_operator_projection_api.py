from __future__ import annotations

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError
from vonk_control.agent_api import EnrollmentGrantResponse
from vonk_control.auth import MUTATION_ROLES, Actor, CursorError
from vonk_control.deployment_provenance_contract import (
    DeploymentProvenance,
    PlatformObservation,
)
from vonk_control.library_api import _error as library_error
from vonk_control.operator_projection_api import (
    FleetNodeDetailResponse,
    FleetOperatorServices,
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
        assert client.post("/api/fleet/enroll", json={"name": "Spark"}).status_code == 403
        assert client.post(
            f"/api/fleet/{_NODE}/rename", json={"display_name": "Spark"}
        ).status_code == 403
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
