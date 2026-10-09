"""The recipe package retries uncertainty and ends gone owners without residue."""

from __future__ import annotations

from importlib import import_module

import pytest
from vonk_agent_protocol import (
    SecurityRefusalError,
    SecurityRefusalReason,
    UnknownOutcomeError,
    WaitReason,
)
from vonk_control.recipe_operations import RecipeOperationService


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
        with pytest.raises(Exception):  # noqa: B017 -- bounded ending and same-input recovery below
            getattr(service, method)(*args, **kwargs)
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
    with pytest.raises(Exception):  # noqa: B017 -- denied authority is never retried or dispatched
        getattr(service, method)(*args, **kwargs)
    assert calls == [(args, kwargs)]
    recovered = object()
    monkeypatch.setattr(service, once, lambda *_args, **_kwargs: recovered)
    assert getattr(service, method)(*args, **kwargs) is recovered


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
