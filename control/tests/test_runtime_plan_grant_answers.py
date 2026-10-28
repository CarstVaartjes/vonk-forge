"""A grant whose plan the records cannot bind answers by behaviour, never 403.

The 2026-10-28 dual DSV4 start: both ranks asked for their Start grant while
the Controller's own records did not bind the plan, and the grant route
answered 403 ``host runtime authority denied``. The agent ends a 403 as a
security failure, so bookkeeping became a terminal refusal. Now a plan the
records do not bind (superseded, its run ended, its run not ready) is a
definite 409 that ends the order at once and leaves the work to its owner;
nothing waits, because no producer readies such a record again for the same
order. A refused binding and a request that differs from the exact plan stay
403.
"""

from __future__ import annotations

import hashlib
import json
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
    canonical_message,
)
from vonk_agent_protocol.recipe_operations import RecipeStartPayload
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
    RuntimePlanEvidenceUnavailable,
    RuntimePlanUnsatisfiable,
)
from vonk_control.models import (
    AgentOperation,
    ClusterMapping,
    Job,
    RecipeInstallation,
    RecipeRun,
)
from vonk_control.recipe_execution_contract import (
    parse_stored_run_plan,
    run_plan_document,
)

from .test_host_helper_authority import (
    FENCE,
    RUNTIME_INSTALLATION_ID,
    RUNTIME_RUN_ID,
    _RuntimePlanBinding,
    runtime_plan_binding,
    runtime_service,
)

_NODE = "spk_" + "1" * 32
_JOB_ID = "20000000-0000-4000-8000-000000000002"
_OPERATION_ID = "30000000-0000-4000-8000-000000000003"


def _ask(
    service: HostRuntimeAuthorityService,
    binding: _RuntimePlanBinding,
    action: ContainerRuntimeAction = ContainerRuntimeAction.START,
) -> SignedHostHelperGrant:
    return service.issue_grant(
        node_id=_NODE,
        fence=FENCE,
        action=action,
        request_sha256="e" * 64,
        certificate_serial="certificate-1",
        **binding,
    )


def _answer(
    service: HostRuntimeAuthorityService, binding: _RuntimePlanBinding
) -> HostHelperAuthorityError:
    with pytest.raises(HostHelperAuthorityError) as answered:
        _ask(service, binding)
    return answered.value


def _installation(session: Session) -> RecipeInstallation:
    installation = session.get(RecipeInstallation, RUNTIME_INSTALLATION_ID)
    assert installation is not None
    return installation


def _run(session: Session) -> RecipeRun:
    run = session.get(RecipeRun, RUNTIME_RUN_ID)
    assert run is not None
    return run


#: What leaves the run not ready for its Start, by its production writer.
#: None is ever readied again for the same order, so none is a wait.
_NOT_READY: dict[str, Callable[[Session], None]] = {
    # ``installed_bytes.py``: a Spark's listing lost the installed ranks.
    "installation-partial": lambda session: setattr(
        _installation(session), "state", InstallationState.PARTIAL
    ),
    # A failed install or an uninstall; only a new request installs again.
    "installation-failed": lambda session: setattr(
        _installation(session), "state", InstallationState.FAILED
    ),
    "installation-uninstalled": lambda session: setattr(
        _installation(session), "state", InstallationState.UNINSTALLED
    ),
}


@pytest.mark.parametrize("cause", sorted(_NOT_READY))
def test_a_start_whose_installation_is_not_installed_ends_definite(cause: str) -> None:
    # Wrong implementation (main): a 403 security refusal; a 503 wait would
    # observe for an hour a record no producer repairs for this order.
    service = runtime_service()
    binding = runtime_plan_binding(service, ContainerRuntimeAction.START)
    with service._sessions.begin() as session:
        _NOT_READY[cause](session)
    answered = _answer(service, binding)
    assert type(answered) is HostRuntimeGrantUnsatisfiable
    assert answered.reason is InvalidRequestReason.NOT_READY


@pytest.mark.parametrize(
    "ended",
    [RunState.STOPPING, RunState.STOPPED, RunState.FAILED, RunState.LOST],
)
def test_a_start_of_a_run_that_ended_is_definite(ended: RunState) -> None:
    # Every writer of these states ends the run; a restart is a new
    # generation, so this Start can never be current again.
    service = runtime_service()
    binding = runtime_plan_binding(service, ContainerRuntimeAction.START)
    with service._sessions.begin() as session:
        _run(session).state = ended
    answered = _answer(service, binding)
    assert type(answered) is HostRuntimeGrantUnsatisfiable
    assert answered.reason is InvalidRequestReason.SUPERSEDED


def _bump_generation(session: Session) -> None:
    # What distributed recovery does (``distributed_recovery/coordinator.py``):
    # the run's generation and its stored plan move to the next generation.
    run = _run(session)
    run.run_generation += 1
    run.plan = run_plan_document(
        parse_stored_run_plan(run.plan).model_copy(
            update={"run_generation": run.run_generation}
        )
    )


def _remap_installation(session: Session) -> None:
    # A remapped installation: its mapping is formed again at a new generation.
    installation = _installation(session)
    mapping = session.get(ClusterMapping, installation.mapping_id)
    assert mapping is not None
    mapping.generation += 1
    installation.mapping_generation = mapping.generation


def _reissue_for_current_generation(session: Session) -> None:
    # The newer order recovery issues: this node's Start of the run's current
    # generation, recorded in its parent's manifest.
    operation = session.get(AgentOperation, _OPERATION_ID)
    job = session.get(Job, _JOB_ID)
    assert operation is not None and job is not None
    start = RecipeStartPayload.model_validate_json(
        canonical_message(operation.payload)
    ).model_copy(update={"run_generation": _run(session).run_generation})
    document = json.loads(canonical_message(start))
    operation.payload = document
    operation.payload_digest = hashlib.sha256(canonical_message(document)).hexdigest()
    manifest = json.loads(canonical_message(job.payload))
    for phase in manifest["phases"]:
        for item in phase:
            if item["operation_id"] == _OPERATION_ID:
                item["payload"] = document
    job.payload = manifest
    job.payload_digest = hashlib.sha256(canonical_message(manifest)).hexdigest()


_SUPERSESSIONS: dict[str, Callable[[Session], None]] = {
    "recovery-generation-bump": _bump_generation,
    "installation-remap": _remap_installation,
}


@pytest.mark.parametrize("event", sorted(_SUPERSESSIONS))
def test_a_superseded_start_ends_at_once_with_a_definite_answer(event: str) -> None:
    # Wrong implementation (main): a 403 security refusal; a 503 would keep
    # the old order observing a plan that can never be current again.
    service = runtime_service()
    binding = runtime_plan_binding(service, ContainerRuntimeAction.START)
    with service._sessions.begin() as session:
        _SUPERSESSIONS[event](session)
    ended = _answer(service, binding)
    assert type(ended) is HostRuntimeGrantUnsatisfiable
    assert ended.reason is InvalidRequestReason.SUPERSEDED


def test_the_newer_generations_start_is_granted_after_the_old_one_ends() -> None:
    service = runtime_service()
    old = runtime_plan_binding(service, ContainerRuntimeAction.START)
    with service._sessions.begin() as session:
        _bump_generation(session)
    assert type(_answer(service, old)) is HostRuntimeGrantUnsatisfiable

    with service._sessions.begin() as session:
        _reissue_for_current_generation(session)
    fresh = runtime_plan_binding(service, ContainerRuntimeAction.START)
    assert fresh["run_generation"] == 2
    operation = _ask(service, fresh).claims.operation
    assert isinstance(operation, ExecuteContainerRuntimeRequestOperation)
    assert operation.run_generation == 2


def test_a_stop_request_without_its_own_document_is_definite() -> None:
    # Wrong implementation (main): a 403 for the agent's own malformed Stop
    # request; it never changes by asking again, so it is no wait either.
    service = runtime_service()
    binding = runtime_plan_binding(service, ContainerRuntimeAction.STOP)
    with pytest.raises(HostHelperAuthorityError) as answered:
        _ask(
            service, {**binding, "stop_plan_sha256": None}, ContainerRuntimeAction.STOP
        )
    assert type(answered.value) is HostRuntimeGrantUnsatisfiable
    assert answered.value.reason is InvalidRequestReason.MALFORMED


def test_a_refused_binding_and_a_differing_request_stay_security_refusals() -> None:
    service = runtime_service()
    binding = runtime_plan_binding(service, ContainerRuntimeAction.START)
    differing: list[_RuntimePlanBinding] = [
        {**binding, "start_plan_sha256": "0" * 64},
        {**binding, "run_generation": 2},
        {**binding, "runtime_installation_id": RUNTIME_RUN_ID},
    ]
    for changed in differing:
        refused = _answer(service, changed)
        assert type(refused) is HostHelperAuthorityError
        assert (
            refused.denial_reason
            is SecurityRefusalReason.HELPER_REQUEST_PLAN_BINDING_INVALID
        )
    # The operation record no longer matches its own digest: refused.
    with service._sessions.begin() as session:
        operation = session.get(AgentOperation, _OPERATION_ID)
        assert operation is not None
        operation.payload_digest = "0" * 64
    refused = _answer(service, binding)
    assert type(refused) is HostHelperAuthorityError
    assert isinstance(refused.__cause__, RuntimePlanAuthorityRefused)


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
    RuntimePlanEvidenceUnavailable: (
        RuntimePlanEvidenceUnavailable(
            "unavailable", reason=WaitReason.OBSERVATION_UNAVAILABLE
        ),
        HostRuntimeEvidenceUnavailable,
    ),
    RuntimePlanUnsatisfiable: (
        RuntimePlanUnsatisfiable("never", reason=InvalidRequestReason.SUPERSEDED),
        HostRuntimeGrantUnsatisfiable,
    ),
}


def test_every_plan_authority_answer_is_classified() -> None:
    assert set(RuntimePlanAuthorityError.__subclasses__()) == set(_ANSWERS)


@pytest.mark.parametrize(
    "action", [ContainerRuntimeAction.START, ContainerRuntimeAction.STOP]
)
@pytest.mark.parametrize(
    "kind",
    [pytest.param(kind, id=kind.__name__) for kind in _ANSWERS],
)
def test_each_plan_authority_answer_reaches_the_grant_as_classified(
    kind: type[RuntimePlanAuthorityError],
    action: ContainerRuntimeAction,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raised, expected = _ANSWERS[kind]

    def answer(*_arguments: object, **_keywords: object) -> object:
        raise raised

    service = runtime_service()
    binding = runtime_plan_binding(service, action)
    monkeypatch.setattr(host_helper_authority, "derive_runtime_plan_binding", answer)
    with pytest.raises(HostHelperAuthorityError) as answered:
        _ask(service, binding, action)
    assert type(answered.value) is expected
