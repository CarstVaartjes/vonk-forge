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
    SECURITY,
    classify,
    propose_families,
)

#: The repository parse is shared setup, not the first test's own time.
pytestmark = pytest.mark.usefixtures("parsed_repository")

PATH = "control/src/vonk_control/sample.py"

# The retry proof walks the repository call graph; build it once per session.
pytestmark = pytest.mark.usefixtures("retry_proof_graph")


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
        (_site("StaleAgentFence", "lease expired"), SECURITY),
        # A lapsed lease is bookkeeping even when its diagnostic mentions
        # authority or integrity. Only an authenticated fence refusal is security.
        (
            _site("StaleAgentLease", "certificate or fence observation unavailable"),
            DEBT,
        ),
        (_site("RecipeRetryLater", "stored digest mismatch"), DEBT),
        (_site("RecipeStopAuthorityRefused", "stored authority unavailable"), SECURITY),
        (_site("SourceBundleError", "bundle.digest_mismatch"), SECURITY),
        (_site("RecipeBuildError", "build.signature_invalid"), SECURITY),
        # An unknown-outcome class is a handoff only where a registered loop is
        # proven to retry it (test_blocker_retries); by type alone it is debt.
        (_site("InstallAdmissionBusy", "install.capacity_busy"), DEBT),
        (_site("RecipeRouteSuperseded", "route publication was superseded"), DEBT),
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


@pytest.mark.parametrize(
    ("cls", "category"),
    [
        ("InvalidValue", INPUT),
        ("MissingRecord", INPUT),
        ("SecurityRefused", SECURITY),
        ("UnsettledOutcome", DEBT),
        ("AdmissionLockBusy", DEBT),
    ],
)
def test_a_class_of_a_category_type_names_its_family(cls: str, category: str) -> None:
    """The error type decides, whatever the message says."""

    assert classify(_site(cls, "stored plan is missing")).category == category


@pytest.mark.parametrize(
    ("function", "code", "category"),
    [
        ("CatalogEntityService.resolve_reference", "catalog.reference_missing", INPUT),
        ("CatalogEntityService.revise", "catalog.revision_missing", DEBT),
        ("_head", "catalog.head_missing", DEBT),
        (
            "CatalogEntityService._bind_recipe_models",
            "catalog.model_artifact_missing",
            DEBT,
        ),
    ],
)
def test_reference_request_validation_does_not_hide_persisted_catalog_debt(
    function: str, code: str, category: str
) -> None:
    """Missing a caller's exact target is distinct from losing stored metadata."""
    document = load_allowlist()
    families = document["fail_closed"]
    assert isinstance(families, list)
    family = next(
        family
        for family in families
        if any(
            site[:4]
            == [
                "control/src/vonk_control/catalog_entities.py",
                "CatalogValidationError",
                function,
                code,
            ]
            for site in family["sites"]
        )
    )
    assert family["category"] == category


def test_supplied_harness_projection_is_distinct_from_compiler_omissions() -> None:
    """A pure input validator must not credit the compiler's missing output."""
    families = load_allowlist()["fail_closed"]
    assert isinstance(families, list)
    categories = {family["family"]: family["category"] for family in families}
    assert categories["common.projection-request"] == INPUT
    assert categories["canonical.image-handle-request"] == INPUT
    assert categories["recipe_runtime_specs.bookkeeping-debt"] == DEBT
