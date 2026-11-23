"""A mutation whose owner row is not there is retryable (503), never a settled 409."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from fastapi import HTTPException, Response
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.requests import Request

from vonk_agent_protocol.agent_words import ProgressPhase
from vonk_agent_protocol.lifecycle_vocabulary import LifecycleState, OperatorActionName
from vonk_agent_protocol.reason_codes.recipes import RecipeImageCode
from vonk_agent_protocol.wire_model import OperationProgress
from vonk_control.api.application import create_app
from vonk_control.auth import Actor, AgentIdentity, TokenCodec
from vonk_control.categorized_errors import MissingRecord
from vonk_control.gateway.keys import GatewayKeyRevoked
from vonk_control.jobs.queue import JobService
from vonk_control.model_primitives import Base
from vonk_control.operator_acceptance import (
    AcceptanceArguments,
    OperatorReceiptResolver,
)
from vonk_control.operator_receipt_contract import ReceiptPolicy
from vonk_control.recipe_image_availability import requests
from vonk_control.recipe_image_availability.contracts import (
    OPERATION_KIND,
    RecipeImageAvailabilityUnknown,
)
from vonk_control.recipe_image_availability.view_contract import (
    RecipeImageAvailabilityView,
)
from vonk_control.recipe_operations.availability_intent import (
    RecipeRevisionIntent,
    RecipeSelectorIntent,
)

from .test_fleet_profile_api import _headers


class _Recipes:
    """The real `_start_request` budget loop over an owner that can be unsure."""

    def __init__(self) -> None:
        self.unsure = True
        self.rows: dict[str, object] = {}
        self.handler_runs = 0
        self._clock = lambda: datetime.now(UTC)

    def start_selector(self, selector: str, *, actor: str, request_id: str):
        self.handler_runs += 1
        return requests._start_request(
            self,  # type: ignore[arg-type]
            RecipeSelectorIntent(selector=selector),
            actor=actor,
            request_id=request_id,
        )

    def get_operator_request(self, request_key: str, *, actor: str):
        if request_key not in self.rows:
            raise MissingRecord(request_key)
        return self.rows[request_key]


def _queued_view(request_id: str) -> RecipeImageAvailabilityView:
    return RecipeImageAvailabilityView(
        id=request_id,
        request_id=request_id,
        request=RecipeRevisionIntent(recipe_revision_id="revision"),
        kind=OPERATION_KIND,
        state=LifecycleState.QUEUED,
        attempt=1,
        recipe_revision_id="revision",
        recipe_content_sha256="a" * 64,
        model_digest=None,
        build_input_sha256=None,
        progress=OperationProgress(phase=ProgressPhase.PREPARING),
        image_progress=None,
        result=None,
        failure=None,
        supported_actions=(),
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    )


def test_unconfirmed_recipe_request_answers_503_then_same_key_is_accepted(
    monkeypatch,
) -> None:
    """Catches an outcome-unknown submission answering a settled view or 409."""
    recipes = _Recipes()
    monkeypatch.setattr(requests, "REQUEST_OBSERVATION_BUDGET_SECONDS", 0)

    def once(service, intent, *, actor, request_id, update_claim):
        if recipes.unsure:
            raise RecipeImageAvailabilityUnknown(
                RecipeImageCode.BUILD_INPUT_MISSING, "build input is not ready yet"
            )
        view = _queued_view(request_id)
        recipes.rows[request_id] = view
        return view

    monkeypatch.setattr(requests, "_start_request_once", once)
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    codec = TokenCodec(b"missing-acceptance-retryable-key32")
    api = TestClient(
        create_app(
            jobs=JobService(sessionmaker(engine), clock=lambda: datetime.now(UTC)),
            tokens=codec,
            now=lambda: 1,
            recipe_image_availability=recipes,
        )
    )
    headers = _headers(codec, "operator")
    body = {"request_key": str(uuid4())}
    refused = api.post("/api/recipe/example/download", json=body, headers=headers)
    assert refused.status_code == 503, refused.text
    assert refused.headers["Retry-After"]
    assert "x-vonk-receipt" not in refused.headers
    recipes.unsure = False
    accepted = api.post("/api/recipe/example/download", json=body, headers=headers)
    assert accepted.status_code == 202, accepted.text
    assert accepted.headers["x-vonk-receipt"]
    assert recipes.handler_runs == 2


def test_agent_receipt_with_no_owner_row_is_retryable() -> None:
    """Catches a missing agent-output owner row escaping the resolver as a 500."""
    identity = AgentIdentity("spk_" + "a" * 32, "serial", "fingerprint", True)
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "headers": [],
            "path_params": {"job_id": "job-1", "sha256": "a" * 64},
            "vonk.agent_identity": identity,
        }
    )

    def missing(_job_id: str) -> object:
        raise MissingRecord("job-1")

    resolver = OperatorReceiptResolver(artifacts=SimpleNamespace(get=missing))  # type: ignore[arg-type]
    arguments = AcceptanceArguments(
        None,
        None,
        None,
        None,
        None,
        OperatorActionName.RESUME,
        request,
        None,
    )
    with pytest.raises(HTTPException) as refused:
        resolver.resolve(
            ReceiptPolicy.AGENT_OUTPUT,
            Response(),
            arguments,
        )
    assert refused.value.status_code == 503


def _resolve_key(receipt: Any) -> HTTPException:
    resolver = OperatorReceiptResolver(keys=SimpleNamespace(receipt=receipt))  # type: ignore[arg-type]
    arguments = AcceptanceArguments(
        Actor("owner", "administrator"),
        None,
        None,
        None,
        None,
        OperatorActionName.RESUME,
        None,
        None,
    )
    created = GatewayKeyRevoked(name="k", request_id=str(uuid4()))
    with pytest.raises(HTTPException) as refused:
        resolver.resolve(ReceiptPolicy.KEY, created, arguments)
    return refused.value


def test_key_receipt_404_after_success_is_retryable() -> None:
    """Catches the gateway-key owner's 404 escaping as a settled not-found."""

    def absent(*_args: object, **_kwargs: object) -> object:
        raise HTTPException(status_code=404)

    refused = _resolve_key(absent)
    assert refused.status_code == 503
    assert refused.headers
    assert refused.headers["retry-after"]


def test_unknown_outcome_owner_is_retryable_not_500() -> None:
    """Catches an UnknownOutcomeError owner (e.g. recipe admission) escaping as 500."""

    def unsure(*_args: object, **_kwargs: object) -> object:
        raise RecipeImageAvailabilityUnknown(
            RecipeImageCode.BUILD_INPUT_MISSING, "build input is not ready yet"
        )

    refused = _resolve_key(unsure)
    assert refused.status_code == 503
    assert refused.headers
    assert refused.headers["retry-after"]
