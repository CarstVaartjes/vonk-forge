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
    "cls", ["AuthError", "CursorError", "InvalidValue", "UnsettledOutcome", "Renamed"]
)
@pytest.mark.parametrize(
    "message", ["invalid signature", "stored record missing", "busy", ""]
)
@pytest.mark.parametrize("path", [PATH, "rust/crates/vonk-agent/src/sample.rs"])
def test_names_text_and_language_never_earn_boundary_credit(cls, message, path):
    assert classify(_site(cls, message, path=path)).category == DEBT


def test_new_sites_are_inventoried_without_automatic_security_credit():
    sites = [
        _site("AuthError", "signature invalid", "a"),
        _site("AuthError", "signature invalid", "a"),
        _site("Renamed", "ready", "b"),
    ]
    families = propose_families(sites, {"fail_closed": []})
    assert len(families) == 1
    assert families[0]["category"] == DEBT
    assert families[0]["sites"] == [
        [PATH, "AuthError", "go", "a", 2],
        [PATH, "Renamed", "go", "b", 1],
    ]


def test_listed_sites_are_not_proposed_again() -> None:
    sites = scan_raises()
    assert propose_families(sites, load_allowlist()) == []


@pytest.mark.parametrize(
    ("function", "code", "category"),
    [
        ("CatalogEntityService.resolve_reference", "catalog.reference_missing", INPUT),
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
