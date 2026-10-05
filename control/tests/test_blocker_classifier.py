"""The classifier proposes a category; these cases pin the rules, not the wording."""

from __future__ import annotations

import pytest

from .blocker_boundaries import (
    CONTROL_SOURCE_ROOT,
    RaiseSite,
    load_allowlist,
    parsed_modules,
    scan_raises,
)
from .blocker_classifier import (
    DEBT,
    INPUT,
    RETRIED,
    SECURITY,
    classify,
    propose_families,
)

PATH = "control/src/vonk_control/sample.py"


@pytest.fixture(scope="module", autouse=True)
def _parsed_control_sources() -> None:
    """Parse control/src once for the module: the scans share it (a shared fixture
    is outside the per-test budget, and the parse is the whole cost)."""

    parsed_modules(CONTROL_SOURCE_ROOT)


def _site(
    cls: str, message: str = "", code: str = "x.y", path: str = PATH
) -> RaiseSite:
    return RaiseSite(path, cls, "go", code, 1, message=message)


@pytest.mark.parametrize(
    ("site", "category"),
    [
        (_site("EnrollmentDenied", "node identity already exists"), SECURITY),
        (_site("StaleAgentAttempt", "fence is stale"), SECURITY),
        (_site("SourceBundleError", "bundle.digest_mismatch"), SECURITY),
        (_site("RecipeBuildError", "build.signature_invalid"), SECURITY),
        (_site("InstallAdmissionBusy", "install.capacity_busy"), RETRIED),
        (_site("RecipeRouteSuperseded", "route publication was superseded"), RETRIED),
        (_site("CursorError", "operation cursor is invalid"), INPUT),
        (_site("HarnessCompileError", "harness mounts overlap"), INPUT),
        (_site("RecipeOperationConflict", "request key was already used"), INPUT),
        (
            _site("LibraryProjectionError", "persisted recipe run state is invalid"),
            DEBT,
        ),
        (_site("RecipeBuildError", "build.source_unavailable"), DEBT),
        (_site("DistributedLifecycleError", "accepted Start plan is invalid"), DEBT),
        (_site("EnrollmentIssuanceUncertain", "issuance is uncertain"), DEBT),
        (_site("SourceBundleError", "bundle.read_failed"), DEBT),
    ],
)
def test_a_site_is_classified_by_class_then_message(
    site: RaiseSite, category: str
) -> None:
    assert classify(site).category == category


def test_a_security_class_beats_a_busy_looking_message() -> None:
    assert classify(_site("AuthError", "token is busy")).category == SECURITY


def test_new_sites_become_one_family_per_module_and_category() -> None:
    document: dict[str, object] = {"fail_closed": []}
    sites = [
        _site("CursorError", "cursor is invalid", "a.b"),
        _site("CursorError", "cursor is invalid", "a.b"),
        _site("RecipeBuildError", "build.source_unavailable", "b.c"),
    ]
    families = propose_families(sites, document)
    assert [(f["family"], f["category"]) for f in families] == [
        ("sample.bookkeeping-debt", DEBT),
        ("sample.input-validation", INPUT),
    ]
    cursor = next(f for f in families if f["category"] == INPUT)
    assert cursor["sites"] == [[PATH, "CursorError", "go", "a.b", 2]]


def test_listed_sites_are_not_proposed_again() -> None:
    sites = scan_raises()
    assert propose_families(sites, load_allowlist()) == []
