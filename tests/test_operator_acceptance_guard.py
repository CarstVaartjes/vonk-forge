"""Mounted operator mutations cannot silently escape receipt enforcement."""

import pytest
from fastapi import APIRouter, Response
from fastapi.testclient import TestClient
from vonk_control.contract_graph import ContractGraphError, _routes, schema_application
from vonk_control.operator_acceptance import (
    ReceiptPolicy,
    operator_acceptance,
    receipt_policy,
    security_edge_policy,
)
from vonk_control.operator_receipt_contract import SecurityEdge
from vonk_control.strict_json import ControllerAPIRoute


def require_operator_policies(app):
    for path, route, _schema in _routes(app):
        if not route.methods & {
            "POST",
            "PUT",
            "PATCH",
            "DELETE",
        }:
            continue
        policy = receipt_policy(route.endpoint)
        if policy is None:
            raise ContractGraphError(
                f"Operator mutation has no acceptance policy: {path}"
            )
        if policy not in {
            ReceiptPolicy.SECURITY_EDGE,
            ReceiptPolicy.OBSERVATION,
        } and not isinstance(route.original_route, ControllerAPIRoute):
            raise ContractGraphError(
                f"Operator mutation does not enforce receipts: {path}"
            )
        if (
            policy == ReceiptPolicy.SECURITY_EDGE
            and security_edge_policy(route.endpoint) is None
        ):
            raise ContractGraphError(f"Unclassified security edge: {path}")


@pytest.mark.parametrize("browser_auth", [False, True])
def test_every_mounted_mutation_has_receipt_semantics(browser_auth):
    """Catches operator routes, including dynamically included routes, with no owner."""
    require_operator_policies(schema_application(browser_auth=browser_auth))


def test_new_unregistered_post_fails_the_mounted_guard():
    """Catches a new schema-valid mutation absent from a hand-maintained route list."""
    app = schema_application()

    @app.post("/api/new-operator-action", response_model=None)
    def new_action() -> Response:
        return Response(status_code=204)

    with pytest.raises(ContractGraphError, match="no acceptance policy"):
        require_operator_policies(app)


@pytest.mark.parametrize("raw_response", [True, False])
def test_actual_no_content_success_cannot_bypass_the_receipt_policy(raw_response):
    """Catches the early Response/204 return bypassing JSON response enforcement."""
    app = schema_application()

    @app.post("/api/test-no-receipt", response_model=None, status_code=204)
    @operator_acceptance(ReceiptPolicy.MODEL_REQUEST)
    def no_receipt():
        return Response(status_code=204) if raw_response else {"accepted": True}

    response = TestClient(app).post("/api/test-no-receipt")
    assert response.status_code == 503
    assert response.headers["x-vonk-error-code"]


def test_declared_policy_on_an_unenforced_router_fails_the_guard():
    """Catches a mounted router whose response bypasses the enforcement class."""
    app = schema_application()
    router = APIRouter()

    @router.post("/api/unenforced", response_model=None, status_code=204)
    @operator_acceptance(ReceiptPolicy.MODEL_REQUEST)
    def no_receipt() -> Response:
        return Response(status_code=204)

    app.include_router(router)
    with pytest.raises(ContractGraphError, match="does not enforce receipts"):
        require_operator_policies(app)


@pytest.mark.parametrize("browser_auth", [False, True])
def test_security_edges_are_exactly_the_reviewed_credential_endpoints(browser_auth):
    """Catches adding an auth route under a broad namespace exemption."""
    app = schema_application(browser_auth=browser_auth)
    edges = [
        security_edge_policy(route.endpoint)
        for _, route, _ in _routes(app)
        if receipt_policy(route.endpoint) == ReceiptPolicy.SECURITY_EDGE
    ]
    assert set(edges) == (set(SecurityEdge) if browser_auth else set())
    assert len(edges) == len(set(edges))


@pytest.mark.parametrize("path", ["/agent/new", "/api/auth/new-token"])
def test_new_agent_or_auth_route_requires_deliberate_classification(path):
    """Catches subtree exemptions, including dynamically mounted auth routes."""
    app = schema_application()
    app.add_api_route(path, lambda: Response(status_code=204), methods=["POST"])
    with pytest.raises(ContractGraphError, match="no acceptance policy"):
        require_operator_policies(app)


def test_new_auth_route_cannot_borrow_a_security_edge_exemption():
    """Catches a generic security-edge policy on an unreviewed issuance path."""
    app = schema_application()

    @app.post("/api/auth/new-token", response_model=None)
    @operator_acceptance(ReceiptPolicy.SECURITY_EDGE)
    def unreviewed():
        return Response(status_code=204)

    with pytest.raises(ContractGraphError, match="Unclassified security edge"):
        require_operator_policies(app)


@pytest.mark.parametrize(
    "policy",
    [
        policy
        for policy in ReceiptPolicy
        if policy not in {ReceiptPolicy.OBSERVATION, ReceiptPolicy.SECURITY_EDGE}
    ],
)
def test_each_mutation_policy_rejects_uncommitted_no_content_success(policy):
    """Catches a policy added to the runtime bypass set or a raw 204 escaping enforcement."""
    app = schema_application()

    @app.post("/api/test-uncommitted", response_model=None, status_code=204)
    @operator_acceptance(policy)
    def unchanged() -> Response:
        return Response(status_code=204)

    response = TestClient(app).post("/api/test-uncommitted")
    assert response.status_code == 503
    assert "x-vonk-receipt" not in response.headers
