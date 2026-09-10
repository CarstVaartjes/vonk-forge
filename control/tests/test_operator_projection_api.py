from __future__ import annotations

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from vonk_control.agent_api import EnrollmentGrantResponse
from vonk_control.auth import MUTATION_ROLES, Actor
from vonk_control.operator_projection_api import (
    FleetOperatorServices,
    build_fleet_operator_services,
    install_operator_projection_routes,
)

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
