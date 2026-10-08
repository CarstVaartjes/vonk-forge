"""Prove the provenance scanner flags each wrong comparison and allows the right ones.

A gate that cannot fail on the wrong implementation is ceremony, so every rule
is exercised against a fixture with the comparison that went wrong in
production (a receipt's ``build_id``, ``distribution_*``, the builder binary in
a build lookup) and against the closest allowed shape.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from .content_identity_boundaries import (
    ALLOWLIST_PATH,
    BUILDER_BINARY_IN_BUILD_INPUT,
    OWNER_MODULE,
    PROVENANCE_COMPARISON,
    PROVENANCE_FILTER,
    Site,
    evaluate_identity_gate,
    load_allowlist,
    scan_provenance_sites,
    scan_source,
)
from .registry_storage import write_registry

#: The repository parse is shared setup, not the first test's own time.
pytestmark = pytest.mark.usefixtures("parsed_repository")


def _scanned(source: str, path: str = "control/src/vonk_control/sample.py"):
    return [
        (site.kind, site.expression) for site in scan_source(dedent(source), path=path)
    ]


@pytest.mark.parametrize(
    "comparison",
    [
        "stored.build_id != build.id",
        "receipt.distribution_slug != revision.slug",
        "receipt.distribution_publisher == plan.publisher",
        "receipt.distribution_content_sha256 != revision.content_digest",
        "receipt.runtime_adapter != adapter.adapter_id",
        "receipt.runtime_adapter_sha256 != adapter.digest",
        "image.recipe_revision_id == revision.id",
        "plan.recipe_id in recipe_ids",
        "payload['build_id'] != owner_id",
        "payload.get('recipe_revision_id') == revision.id",
        "verified_build_id not in builds",
    ],
)
def test_a_provenance_comparison_is_a_site(comparison: str) -> None:
    sites = _scanned(
        f"def check(stored, receipt, plan, image, payload):\n    return {comparison}\n"
    )
    assert [kind for kind, _ in sites] == [PROVENANCE_COMPARISON]


def test_a_query_filter_on_provenance_is_a_site() -> None:
    sites = _scanned(
        """
        def find(session, revision):
            session.scalars(select(Build).where(Build.recipe_revision_id == revision.id))
            session.scalars(select(Build).where(Build.build_id.in_(ids)))
            session.scalars(select(Build).filter_by(recipe_id=revision.document_id))
        """
    )
    assert [kind for kind, _ in sites] == [
        PROVENANCE_COMPARISON,
        PROVENANCE_FILTER,
        PROVENANCE_FILTER,
    ]


def test_the_builder_binary_in_a_build_lookup_is_a_site() -> None:
    """The lookup that broke after the Spark agent was upgraded (#1087)."""

    sites = _scanned(
        """
        def reusable(candidate, resolution, policy):
            return candidate.build_input_sha256 == resolution.build_input_for_builder(
                policy.builder_binary_digest
            )
        """
    )
    assert {kind for kind, _ in sites} == {
        PROVENANCE_COMPARISON,
        BUILDER_BINARY_IN_BUILD_INPUT,
    }
    # A builder binary beside no build input is the agent's own package check.
    assert (
        _scanned(
            "def check(node, source):\n"
            "    return node.binary_digest != source.package.binary_sha256\n"
        )
        == []
    )


@pytest.mark.parametrize(
    "comparison",
    [
        "receipt.build_id is None",
        "receipt.distribution_slug == ''",
        "slug == 'vllm'",
        "slug in {'vllm', 'sglang'}",
        "plan.recipe_revision_id != None",
        "receipt.image_digest != plan.image_digest",
        "receipt.oci_archive_sha256 == archive",
        "candidate.build_input_sha256 != plan.build_input_sha256",
        "node.binary_digest != source.package.binary_sha256",
    ],
)
def test_content_existence_and_selector_comparisons_are_not_sites(
    comparison: str,
) -> None:
    assert (
        _scanned(
            f"def check(receipt, plan, slug, candidate, node, source, archive):\n    return {comparison}\n"
        )
        == []
    )


def test_a_slug_beside_the_rest_of_a_catalog_identity_is_a_site() -> None:
    sites = _scanned(
        "def check(a, b):\n"
        "    return (a.publisher, a.slug, a.content_sha256) != (b.publisher, b.slug, b.content_sha256)\n"
    )
    assert [kind for kind, _ in sites] == [PROVENANCE_COMPARISON]


def test_the_owner_module_is_not_scanned() -> None:
    source = "def same(a, b):\n    return a.build_id == b.build_id\n"
    assert _scanned(source, path=OWNER_MODULE) == []
    assert _scanned(source) != []


def test_a_nested_comparison_is_reported_once_by_its_function() -> None:
    sites = scan_source(
        dedent(
            """
            class Service:
                def check(self, receipt, build):
                    def inner():
                        return receipt.build_id != build.id
                    return inner()
            """
        ),
        path="control/src/vonk_control/sample.py",
    )
    assert [site.function for site in sites] == ["Service.check.inner"]


def test_the_controller_compares_no_provenance_outside_the_reviewed_sites() -> None:
    """The gate itself: no new site, and no stale allowlist entry."""

    messages = evaluate_identity_gate(scan_provenance_sites(), load_allowlist())
    assert messages == []


def test_gate_rejects_a_new_site_and_a_stale_entry() -> None:
    reviewed = Site(
        path="control/src/vonk_control/sample.py",
        function="check",
        kind=PROVENANCE_COMPARISON,
        expression="stored.build_id != build.id",
        line=10,
    )
    allowlist = [{**reviewed.identity, "reason": "ownership lookup of one build"}]
    assert evaluate_identity_gate([reviewed], allowlist) == []
    # Moving the code does not break an entry: it is keyed on no line number.
    moved = Site(**{**reviewed.__dict__, "line": 500})
    assert evaluate_identity_gate([moved], allowlist) == []
    fresh = Site(**{**reviewed.__dict__, "expression": "stored.recipe_id != other"})
    messages = evaluate_identity_gate([fresh], allowlist)
    assert any("allowlist with a reason" in message for message in messages)
    assert any("no longer occurs" in message for message in messages)


def test_allowlist_loader_rejects_an_unexplained_or_unknown_entry(
    tmp_path: Path,
) -> None:
    entry = {
        "path": "control/src/vonk_control/sample.py",
        "function": "check",
        "kind": PROVENANCE_COMPARISON,
        "expression": "a.build_id != b.build_id",
    }

    def write(document: dict) -> Path:
        path = tmp_path / "allowlist.json"
        write_registry(path, document)
        return path

    with pytest.raises(ValueError, match="written reason"):
        load_allowlist(write({"schema": 1, "sites": [{**entry, "reason": ""}]}))
    with pytest.raises(ValueError, match="written reason"):
        load_allowlist(write({"schema": 1, "sites": [entry]}))
    with pytest.raises(ValueError, match="unknown site kind"):
        load_allowlist(
            write({"schema": 1, "sites": [{**entry, "kind": "x", "reason": "a b c d"}]})
        )
    assert load_allowlist(
        write(
            {"schema": 1, "sites": [{**entry, "reason": "ownership lookup of a build"}]}
        )
    )


def test_every_committed_entry_names_its_reason() -> None:
    entries = load_allowlist(ALLOWLIST_PATH)
    assert entries
    assert all(len(str(entry["reason"]).split()) >= 8 for entry in entries)
