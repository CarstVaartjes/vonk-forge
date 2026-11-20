"""A mutation that returned without writing its owner row is retryable, not a 409."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from vonk_agent_protocol import LifecycleState, OperatorActionName
from vonk_control.auth import Actor
from vonk_control.categorized_errors import MissingRecord
from vonk_control.operator_acceptance import (
    AcceptanceArguments,
    OperatorReceiptResolver,
)
from vonk_control.operator_projection_api.contracts import FleetActionResponse
from vonk_control.operator_receipt_contract import ReceiptPolicy

KEY = "11111111-1111-4111-8111-111111111111"
DIGEST = "a" * 64


def test_missing_job_is_retryable_then_resolves_once_written() -> None:
    """Catches MissingRecord (an InvalidRequestError) answering a permanent 409."""
    actor = Actor("owner", "administrator")
    rows: dict[str, object] = {}

    def get(identity: str) -> object:
        if identity not in rows:
            raise MissingRecord(identity)
        return rows[identity]

    resolver = OperatorReceiptResolver(jobs=SimpleNamespace(get=get))  # type: ignore[arg-type]
    result = FleetActionResponse(
        action="upgrade",
        state=LifecycleState.QUEUED,
        operation_id="job-1",
        plan_digest=DIGEST,
    )
    arguments = AcceptanceArguments(
        actor, KEY, None, None, None, OperatorActionName.RESUME, None, None
    )
    with pytest.raises(HTTPException) as refused:
        resolver.resolve(ReceiptPolicy.FLEET_ACTION, result, arguments)
    assert refused.value.status_code == 503
    assert refused.value.headers and refused.value.headers["retry-after"]
    rows["job-1"] = SimpleNamespace(
        request_id=KEY, actor="owner", payload_digest=DIGEST
    )
    receipt = resolver.resolve(ReceiptPolicy.FLEET_ACTION, result, arguments)
    assert receipt.id == "job-1" and receipt.request_id == KEY
