"""An unknown-outcome raise counts as retried only where a loop is proven."""

from __future__ import annotations

import ast
import copy

from .blocker_boundaries import RaiseSite, load_allowlist
from .blocker_classifier import DEBT, RETRIED, classify
from .blocker_retries import (
    _handlers_catching,
    evaluate_retry_gate,
    loop_problems,
    proven,
    unknown_classes,
    unproven_sites,
)

PATH = "control/src/vonk_control/fleet_profiles.py"


def _site(exception_class: str, function: str) -> RaiseSite:
    return RaiseSite(PATH, exception_class, function, "x.y", 1, "message")


def test_the_registered_loops_exist_and_name_what_they_retry() -> None:
    assert loop_problems(load_allowlist()) == []


def test_every_already_retried_unknown_raise_has_a_proven_loop() -> None:
    assert evaluate_retry_gate(load_allowlist()) == []


def test_an_unknown_class_is_debt_where_no_loop_catches_it() -> None:
    document = load_allowlist()
    assert "FleetProfileAdmissionBusy" in unknown_classes()
    # The class is unknown-outcome, but nothing proves this function is retried.
    verdict = classify(_site("FleetProfileAdmissionBusy", "no_such_function"), document)
    assert verdict.category == DEBT


def test_an_unknown_class_is_retried_where_a_registered_loop_catches_it() -> None:
    document = load_allowlist()
    function = "FleetProfileService._prepare_pending_admission"
    assert proven(document, PATH, "FleetProfileAdmissionEffectBusy", function)
    verdict = classify(_site("FleetProfileAdmissionEffectBusy", function), document)
    assert verdict.category == RETRIED


def test_without_the_loop_the_same_claim_is_refused() -> None:
    document = copy.deepcopy(load_allowlist())
    retried = [
        family
        for family in document["fail_closed"]  # type: ignore[attr-defined]
        if family["category"] == "already-retried"
    ]
    assert retried
    document["retry_loops"] = []
    assert evaluate_retry_gate(document)
    assert any(unproven_sites(document, family["sites"]) for family in retried)


def test_a_handler_that_raises_is_not_a_retry() -> None:
    names = frozenset({"SomeUnknown"})
    retrying = ast.parse("try:\n    f()\nexcept SomeUnknown:\n    pass\n").body[0]
    raising = ast.parse("try:\n    f()\nexcept SomeUnknown:\n    raise\n").body[0]
    broad = ast.parse("try:\n    f()\nexcept Exception:\n    pass\n").body[0]
    assert _handlers_catching(retrying, names)  # type: ignore[arg-type]
    assert not _handlers_catching(raising, names)  # type: ignore[arg-type]
    assert not _handlers_catching(broad, names)  # type: ignore[arg-type]
