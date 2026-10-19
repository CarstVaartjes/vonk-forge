"""The public removal review selects fleet scope and returns a durable owner."""

from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker
from vonk_agent_protocol import LifecycleState
from vonk_control.model_cache import ModelCacheService
from vonk_control.models import Base, User
from vonk_control.operator_receipt_contract import AcceptanceReceipt, ReceiptOwner

from .test_model_removal_worker_postgres import _prepare_removal_target
from .test_operator_acceptance_postgres import DropFirstResponse, _api


def test_fleet_review_and_receipt_are_owned_by_the_same_durable_removal(
    postgres_engine, tmp_path: Path
) -> None:
    """Catches NAS-only scope, uncommitted acceptance and receipts for refused cancels."""
    Base.metadata.create_all(postgres_engine)
    sessions = sessionmaker(postgres_engine, expire_on_commit=False)
    with sessions.begin() as session:
        session.add(User(subject="operator", role="operator"))
    root = tmp_path / "cache"
    service = ModelCacheService(sessions, root, reserve_bytes=0, fixture_sources=True)
    try:
        selector, model_digest, path = _prepare_removal_target(
            service, sessions, tmp_path, slug="fleet-api", data=b"finished"
        )
        client, headers = _api(sessions, model_cache=service)
        with client:
            review = client.get(
                f"/api/model/{selector}/remove-review",
                params={"fleet": True},
                headers=headers,
            )
            assert review.status_code == 200
            effects = review.json()["fleet"]
            assert effects is not None and effects["nas_remove"] is True
            assert path.read_bytes() == b"finished"
            key = str(uuid4())
            body = {
                "request_key": key,
                "fleet": True,
                "review_digest": review.json()["review_digest"],
            }
            remove_path = f"/api/model/{selector}/remove"
            with pytest.raises(httpx.ReadError):
                TestClient(DropFirstResponse(client.app, remove_path)).post(
                    remove_path, json=body, headers=headers
                )
        service.close()
        service = ModelCacheService(
            sessions, root, reserve_bytes=0, fixture_sources=True
        )
        client, headers = _api(sessions, model_cache=service)
        with client:
            accepted = client.post(remove_path, json=body, headers=headers)
            assert accepted.status_code == 202, accepted.text
            reference = AcceptanceReceipt.model_validate_json(
                accepted.headers["x-vonk-receipt"]
            )
            assert reference.owner == ReceiptOwner.MODEL_CACHE
            assert reference.request_id == key
            receipt = accepted.json()
            assert reference.id == receipt["operation_id"]
            assert receipt["fleet"]["effects"] == effects
            assert receipt["model_content_sha256"] == model_digest
            assert receipt["review_digest"] == review.json()["review_digest"]
            assert receipt["state"] == LifecycleState.QUEUED
            lookup = client.get(reference.href, headers=headers)
            assert lookup.status_code == 200
            assert lookup.json() == receipt
            cancel_path = f"/api/model/operations/{reference.id}/cancel"
            cancel_body = {"request_key": str(uuid4()), "reason": "Keep the model"}
            cancelled = client.post(cancel_path, json=cancel_body, headers=headers)
            assert cancelled.status_code == 409, cancelled.text
            assert "x-vonk-receipt" not in cancelled.headers
            lookup = client.get(reference.href, headers=headers)
            assert lookup.status_code == 200
            assert lookup.json()["fleet"]["effects"] == effects
            assert path.read_bytes() == b"finished"
    finally:
        service.close()
