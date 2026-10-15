"""The host-runtime grant route answers by the authority's typed cause.

A plan that can never bind this attempt is a definite answer the agent ends
the work with; evidence that is not readable yet is a retry-later answer.
"""

from __future__ import annotations

from vonk_agent_protocol import InvalidRequestReason
from vonk_control.host_helper_authority import (
    HostRuntimeEvidenceUnavailable,
    HostRuntimeGrantUnsatisfiable,
)

from .test_agent_api import NODE_A, agent_headers, make_agent_system

_BODY = {
    "fence": "00000000-0000-4000-8000-000000000004",
    "action": "start",
    "request_sha256": "a" * 64,
    "start_plan_sha256": "b" * 64,
    "run_generation": 1,
    "runtime_run_id": "10000000-0000-4000-8000-000000000001",
    "runtime_target_id": "10000000-0000-4000-8000-000000000001",
    "runtime_installation_id": "20000000-0000-4000-8000-000000000002",
    "expires_in_seconds": 30,
}


class _Authority:
    def __init__(self, error: Exception) -> None:
        self.error = error
        self.asks = 0

    def issue_grant(self, **_arguments: object) -> object:
        self.asks += 1
        raise self.error


def _ask(tmp_path, error: Exception):
    client, services, _codec, _clock = make_agent_system(tmp_path)
    authority = _Authority(error)
    object.__setattr__(services, "host_runtime_authority", authority)
    response = client.post(
        "/agent/host-runtime/grant",
        headers=agent_headers(NODE_A, "serial-a"),
        json=_BODY,
    )
    return response, authority.asks


def test_a_plan_that_can_never_bind_is_a_definite_typed_answer(tmp_path) -> None:
    response, asks = _ask(
        tmp_path, HostRuntimeGrantUnsatisfiable(InvalidRequestReason.INCOMPLETE)
    )
    assert response.status_code == 409
    assert response.headers["x-vonk-error-code"] == "incomplete"
    assert "grant" not in response.json()
    assert asks == 1


def test_unreadable_plan_evidence_is_retried_later_not_refused(tmp_path) -> None:
    response, asks = _ask(
        tmp_path, HostRuntimeEvidenceUnavailable("evidence is unavailable")
    )
    assert response.status_code == 503
    assert int(response.headers["retry-after"]) >= 1
    assert "grant" not in response.json()
    assert asks > 1
