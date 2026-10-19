"""Mounted operator mutations cannot silently escape receipt enforcement."""

import pytest
from fastapi import Response
from fastapi.testclient import TestClient
from vonk_control.contract_graph import ContractGraphError, _routes, schema_application
from vonk_control.operator_acceptance import (
    ReceiptPolicy,
    operator_acceptance,
    receipt_policy,
)
from vonk_control.strict_json import route_endpoint


def require_operator_policies(app):
    for path, route, _schema in _routes(app):
        if not path.startswith("/api/") or not route.methods & {
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
        if (
            policy == ReceiptPolicy.AUTH
            and route_endpoint(route.endpoint).__module__ != "vonk_control.auth_api"
        ):
            raise ContractGraphError(
                f"Operator mutation cannot delegate its receipt to auth: {path}"
            )


@pytest.mark.parametrize("browser_auth", [False, True])
def test_every_mounted_operator_mutation_has_receipt_semantics(browser_auth):
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


def test_actual_no_content_success_cannot_bypass_the_receipt_policy():
    """Catches the early Response/204 return bypassing JSON response enforcement."""
    app = schema_application()

    @app.post("/api/test-no-receipt", response_model=None, status_code=204)
    @operator_acceptance(ReceiptPolicy.MODEL_REQUEST)
    def no_receipt() -> Response:
        return Response(status_code=204)

    response = TestClient(app).post("/api/test-no-receipt")
    assert response.status_code == 503
    assert response.headers["x-vonk-error-code"]
