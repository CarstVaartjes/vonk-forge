"""The recipe package retries uncertainty and ends gone owners without residue."""

from __future__ import annotations

from importlib import import_module
from uuid import uuid4

import pytest
from sqlalchemy import select
from vonk_agent_protocol import (
    InvalidRequestReason,
    ReservationState,
    SecurityRefusalError,
    SecurityRefusalReason,
    UnknownOutcomeError,
    WaitReason,
)
from vonk_control.job_documents import RecipeStartParent
from vonk_control.lifecycle.recipe_operation import RecipeOperationAdapter
from vonk_control.models import Job, RecipeRun, ResourceReservation, RunNode
from vonk_control.recipe_operations import RecipeOperationService
from vonk_control.stored_json import read_row_column

from .non_blocking import assert_ended_without_blocking
from .test_recipe_operation_bookkeeping import _running_recipe


@pytest.mark.parametrize(
    "method,once,args,kwargs",
    [
        ("check_build_source", "_check_build_source_once", ("revision",), {}),
        ("preview_build", "_preview_build_once", ("revision", "builder"), {}),
        (
            "build",
            "_build_once",
            ("exact-plan",),
            {
                "build_input_sha256": "exact-digest",
                "actor": "admin",
                "request_id": "request",
                "force": False,
                "admission_guard": None,
            },
        ),
        (
            "start",
            "_start_once",
            ("exact-plan",),
            {
                "plan_digest": "exact-digest",
                "actor": "admin",
                "request_id": "request",
                "workload_intent_ordinal": None,
                "profile_application_id": None,
            },
        ),
        (
            "preview_uninstall",
            "_preview_uninstall_once",
            ("installation",),
            {"also_removing": ()},
        ),
        (
            "retry",
            "_retry_once",
            ("operation",),
            {"actor": "admin", "request_id": "request"},
        ),
    ],
)
@pytest.mark.parametrize("uncertain", [2, 3])
def test_request_reobserves_identical_input_with_a_finite_budget(
    method, once, args, kwargs, uncertain, monkeypatch
):
    service = object.__new__(RecipeOperationService)
    owner = import_module(getattr(service, method).__module__)
    calls = []
    recovered = object()
    failure = UnknownOutcomeError(
        "receipt unavailable", reason=WaitReason.OBSERVATION_UNAVAILABLE
    )

    def observe(*received, **options):
        calls.append((received, options))
        if len(calls) <= uncertain:
            raise failure
        return recovered

    monkeypatch.setattr(owner, "admission_attempts", lambda: iter(range(3)))
    monkeypatch.setattr(service, once, observe, raising=False)
    if uncertain == 3:
        with pytest.raises(UnknownOutcomeError) as ended:
            getattr(service, method)(*args, **kwargs)
        assert ended.value is failure
        assert ended.value.typed_reason == WaitReason.OBSERVATION_UNAVAILABLE
        # Exhaustion retains no poisoned request state: a fresh observation succeeds.
        assert getattr(service, method)(*args, **kwargs) is recovered
    else:
        assert getattr(service, method)(*args, **kwargs) is recovered
    assert calls[:3] == [(args, kwargs)] * 3


@pytest.mark.parametrize(
    "method,once,args,kwargs",
    [
        ("check_build_source", "_check_build_source_once", ("revision",), {}),
        ("preview_build", "_preview_build_once", ("revision", "builder"), {}),
        (
            "build",
            "_build_once",
            ("exact-plan",),
            {
                "build_input_sha256": "exact-digest",
                "actor": "admin",
                "request_id": "request",
                "force": False,
                "admission_guard": None,
            },
        ),
        (
            "start",
            "_start_once",
            ("exact-plan",),
            {
                "plan_digest": "exact-digest",
                "actor": "admin",
                "request_id": "request",
                "workload_intent_ordinal": None,
                "profile_application_id": None,
            },
        ),
        (
            "preview_uninstall",
            "_preview_uninstall_once",
            ("installation",),
            {"also_removing": ()},
        ),
        (
            "retry",
            "_retry_once",
            ("operation",),
            {"actor": "admin", "request_id": "request"},
        ),
    ],
)
def test_request_does_not_retry_an_authority_refusal(
    method, once, args, kwargs, monkeypatch
):
    service = object.__new__(RecipeOperationService)
    calls = []

    def refuse(*received, **options):
        calls.append((received, options))
        raise SecurityRefusalError(
            "authority changed", reason=SecurityRefusalReason.STALE_FENCE
        )

    monkeypatch.setattr(service, once, refuse, raising=False)
    with pytest.raises(SecurityRefusalError) as refused:
        getattr(service, method)(*args, **kwargs)
    assert refused.value.typed_reason == SecurityRefusalReason.STALE_FENCE
    assert calls == [(args, kwargs)]


def test_gone_retirement_owner_releases_claims_and_admits_a_fresh_run(tmp_path):
    sessions, service, _queue, installation, started, _nodes = _running_recipe(tmp_path)
    with sessions.begin() as session:
        original = session.get(Job, started.id)
        assert original is not None
        parent = read_row_column(original, "payload")
        assert isinstance(parent, RecipeStartParent)
        ordinal = parent.workload_intent_ordinal
        assert ordinal is not None
        for node in session.scalars(
            select(RunNode).where(RunNode.run_id == started.owner_id)
        ):
            session.delete(node)
        run = session.get(RecipeRun, started.owner_id)
        assert run is not None
        session.delete(run)
        RecipeOperationAdapter().cancelled(original, service._clock())

    def release():
        with sessions() as session:
            assert not session.scalar(
                select(ResourceReservation.id).where(
                    ResourceReservation.owner_id == started.owner_id,
                    ResourceReservation.state == ReservationState.ACTIVE,
                )
            )

    def end(_receipt):
        reason, completed, advanced = service._retirement_cleanup(
            original, str(uuid4()), "recipe.stop", "run", started.owner_id, ordinal
        )
        assert completed and not advanced
        assert reason.startswith(InvalidRequestReason.NOT_FOUND.value)
        return service.get(original.id)

    def fresh(_world):
        plan = service.preview_run(installation.owner_id, "after-gone-owner")
        assert plan.allowed
        return service.start(
            plan, plan_digest=plan.plan_digest, actor="admin", request_id=str(uuid4())
        )

    assert_ended_without_blocking(
        sessions,
        started,
        end=end,
        fresh=fresh,
        assert_released=release,
        request_key=lambda receipt: receipt.id,
    )


@pytest.mark.parametrize(
    "authority,plan,evidence",
    [
        ("malformed", "a" * 64, "b" * 64),
        ("AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA", "a" * 64, "b" * 64),
        ("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", "not-a-digest", "b" * 64),
    ],
)
def test_publication_input_validation_leaves_a_fresh_input_eligible(
    authority, plan, evidence
):
    # The wrong implementation treats caller syntax as stored-state damage,
    # or keeps a busy publication owner after rejecting the input.
    from vonk_control.route_runtime import AtomicRouteBundlePublisher, RouteRuntimeError

    with pytest.raises(RouteRuntimeError):
        AtomicRouteBundlePublisher._identity(authority, plan, evidence)
    AtomicRouteBundlePublisher._identity(
        "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", "a" * 64, "b" * 64
    )
