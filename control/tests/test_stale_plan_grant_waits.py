"""A Start whose durable authority moved on waits; it is never a security refusal.

The 2026-10-28 dual DSV4 start: both ranks asked for their Start grant while
the Controller's own records said the plan was not current yet (or any more),
and the grant route answered 403 ``host runtime authority denied``. The agent
ends a 403 as a security failure, so a transient conflict became a terminal
refusal. Stale authority is the agent's wait (503, ask again, bounded by the
attempt); a refused binding and a request that differs from the exact plan
stay 403.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from sqlalchemy.orm import Session
from vonk_agent_protocol import (
    ContainerRuntimeAction,
    ExecuteContainerRuntimeRequestOperation,
    InstallationState,
    InvalidRequestReason,
    RunState,
    SecurityRefusalReason,
    SignedHostHelperGrant,
    WaitReason,
)
from vonk_control import host_helper_authority
from vonk_control.host_helper_authority import (
    HostHelperAuthorityError,
    HostRuntimeAuthorityService,
    HostRuntimeEvidenceUnavailable,
    HostRuntimeGrantUnsatisfiable,
)
from vonk_control.host_runtime_plan_authority import (
    RuntimePlanAuthorityError,
    RuntimePlanAuthorityRefused,
    RuntimePlanAuthorityStale,
    RuntimePlanEvidenceUnavailable,
    RuntimePlanUnsatisfiable,
)
from vonk_control.models import AgentOperation, RecipeInstallation, RecipeRun

from .test_host_helper_authority import (
    FENCE,
    RUNTIME_INSTALLATION_ID,
    RUNTIME_RUN_ID,
    _RuntimePlanBinding,
    runtime_plan_binding,
    runtime_service,
)

_NODE = "spk_" + "1" * 32
_OPERATION_ID = "30000000-0000-4000-8000-000000000003"


def _ask_start(
    service: HostRuntimeAuthorityService, binding: _RuntimePlanBinding
) -> SignedHostHelperGrant:
    return service.issue_grant(
        node_id=_NODE,
        fence=FENCE,
        action=ContainerRuntimeAction.START,
        request_sha256="e" * 64,
        certificate_serial="certificate-1",
        **binding,
    )


def _installation(session: Session) -> RecipeInstallation:
    installation = session.get(RecipeInstallation, RUNTIME_INSTALLATION_ID)
    assert installation is not None
    return installation


def _run(session: Session) -> RecipeRun:
    run = session.get(RecipeRun, RUNTIME_RUN_ID)
    assert run is not None
    return run


def _set(
    row: Callable[[Session], object], field: str, value: object
) -> Callable[[Session], None]:
    return lambda session: setattr(row(session), field, value)


#: (drift, heal): the Controller's own records move on, then settle again.
_DRIFTS = {
    "installation-not-installed": (
        _set(_installation, "state", InstallationState.PARTIAL),
        _set(_installation, "state", InstallationState.INSTALLED),
    ),
    "run-no-longer-starting": (
        _set(_run, "state", RunState.STOPPING),
        _set(_run, "state", RunState.STARTING),
    ),
    "run-plan-superseded": (
        _set(_run, "plan_digest", "f" * 64),
        _set(_run, "plan_digest", "a" * 64),
    ),
}


@pytest.mark.parametrize("drift", sorted(_DRIFTS))
def test_a_start_whose_authority_moved_on_waits_then_is_granted(drift: str) -> None:
    # Wrong implementation (main): the stale plan was a 403 security refusal
    # (``helper_request_plan_binding_invalid``) the agent ends the start with.
    move_on, settle = _DRIFTS[drift]
    service = runtime_service()
    binding = runtime_plan_binding(service, ContainerRuntimeAction.START)
    with service._sessions.begin() as session:
        move_on(session)
    with pytest.raises(HostHelperAuthorityError) as answered:
        _ask_start(service, binding)
    assert type(answered.value) is HostRuntimeEvidenceUnavailable
    assert isinstance(answered.value.__cause__, RuntimePlanAuthorityStale)

    # The records settle: the same exact request is granted, no new attempt.
    with service._sessions.begin() as session:
        settle(session)
    grant = _ask_start(service, binding)
    operation = grant.claims.operation
    assert isinstance(operation, ExecuteContainerRuntimeRequestOperation)
    assert operation.start_plan_sha256 == binding["start_plan_sha256"]


def test_a_refused_binding_and_a_differing_request_stay_security_refusals() -> None:
    service = runtime_service()
    binding = runtime_plan_binding(service, ContainerRuntimeAction.START)
    differing: list[_RuntimePlanBinding] = [
        {**binding, "start_plan_sha256": "0" * 64},
        {**binding, "run_generation": 2},
        {**binding, "runtime_installation_id": RUNTIME_RUN_ID},
    ]
    for changed in differing:
        with pytest.raises(HostHelperAuthorityError) as refused:
            _ask_start(service, changed)
        assert type(refused.value) is HostHelperAuthorityError
        assert (
            refused.value.denial_reason
            is SecurityRefusalReason.HELPER_REQUEST_PLAN_BINDING_INVALID
        )
    # The operation record no longer matches its own digest: refused.
    with service._sessions.begin() as session:
        operation = session.get(AgentOperation, _OPERATION_ID)
        assert operation is not None
        operation.payload_digest = "0" * 64
    with pytest.raises(HostHelperAuthorityError) as refused:
        _ask_start(service, binding)
    assert type(refused.value) is HostHelperAuthorityError
    assert isinstance(refused.value.__cause__, RuntimePlanAuthorityRefused)


#: What each typed plan-authority answer becomes at the grant: one row per
#: concrete subclass, so a new subclass cannot fall into the security refusal
#: unreviewed.
_ANSWERS: dict[type[RuntimePlanAuthorityError], tuple[Exception, type]] = {
    RuntimePlanAuthorityRefused: (
        RuntimePlanAuthorityRefused(
            "refused",
            reason=SecurityRefusalReason.HELPER_REQUEST_PLAN_BINDING_INVALID,
        ),
        HostHelperAuthorityError,
    ),
    RuntimePlanAuthorityStale: (
        RuntimePlanAuthorityStale("stale", reason=InvalidRequestReason.SUPERSEDED),
        HostRuntimeEvidenceUnavailable,
    ),
    RuntimePlanEvidenceUnavailable: (
        RuntimePlanEvidenceUnavailable(
            "unavailable", reason=WaitReason.OBSERVATION_UNAVAILABLE
        ),
        HostRuntimeEvidenceUnavailable,
    ),
    RuntimePlanUnsatisfiable: (
        RuntimePlanUnsatisfiable("never", reason=InvalidRequestReason.INCOMPLETE),
        HostRuntimeGrantUnsatisfiable,
    ),
}


def test_every_plan_authority_answer_is_classified() -> None:
    assert set(RuntimePlanAuthorityError.__subclasses__()) == set(_ANSWERS)


@pytest.mark.parametrize(
    "kind",
    [pytest.param(kind, id=kind.__name__) for kind in _ANSWERS],
)
def test_each_plan_authority_answer_reaches_the_grant_as_classified(
    kind: type[RuntimePlanAuthorityError], monkeypatch: pytest.MonkeyPatch
) -> None:
    raised, expected = _ANSWERS[kind]

    def answer(*_arguments: object, **_keywords: object) -> object:
        raise raised

    service = runtime_service()
    binding = runtime_plan_binding(service, ContainerRuntimeAction.START)
    monkeypatch.setattr(host_helper_authority, "derive_runtime_plan_binding", answer)
    with pytest.raises(HostHelperAuthorityError) as answered:
        _ask_start(service, binding)
    assert type(answered.value) is expected
