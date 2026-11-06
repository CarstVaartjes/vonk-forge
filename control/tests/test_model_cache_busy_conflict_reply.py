"""A busy-owner conflict is answered as a wait the client reads as a wait."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
from vonk_control.model_cache.errors import (
    ModelCacheConflictInvalid,
    ModelCacheConflictUnknown,
)
from vonk_control.model_cache_api import model_cache_http_error
from vonk_control.strict_json import ControllerAPIRoute

from cluster_profiles.control_client.common import (
    DISPOSITION_WAIT,
    observation_disposition,
)
from cluster_profiles.control_client.errors import ControlHTTPError


def _app(error: BaseException) -> TestClient:
    app = FastAPI()
    app.router.route_class = ControllerAPIRoute

    @app.post("/busy")
    def busy() -> None:
        raise model_cache_http_error(error)

    return TestClient(app)


def test_busy_owner_conflict_is_a_wait_for_the_client() -> None:
    response = _app(
        ModelCacheConflictUnknown("artifact.deletion_busy", "has another removal owner")
    ).post("/busy")

    assert response.status_code == 503
    assert response.headers["x-vonk-error-code"] == "artifact.deletion_busy"
    after = int(response.headers["retry-after"])
    assert after > 0
    client_error = ControlHTTPError(
        response.status_code,
        response.json()["detail"],
        after,
        code=response.headers["x-vonk-error-code"],
    )
    assert observation_disposition(client_error) == DISPOSITION_WAIT


def test_invalid_conflict_stays_a_409() -> None:
    response = _app(ModelCacheConflictInvalid("model.bad", "no")).post("/busy")

    assert response.status_code == 409
