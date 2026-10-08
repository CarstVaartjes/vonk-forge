"""Regression fixtures for Rust endings and the shared only-falling ratchet."""

import pytest

from .blocker_boundaries import evaluate_raise_gate, write_counts
from .blocker_classifier import (
    DEBT,
    INPUT,
    RETRIED,
    SECURITY,
    classify,
    propose_families,
)
from .blocker_rust import scan_rust_source, tokenize

PATH = "rust/crates/vonk-agent/src/fixture.rs"


@pytest.mark.parametrize(
    "source",
    [
        "fn ingress() { if !signature_valid { return Err(Denied); } }",
        'fn request() { if input.is_empty() { anyhow::bail!("empty caller input"); } }',
        "fn observed() { Err(Unknown) }",
        'fn stored() { ensure!(receipt.is_some(), "missing receipt"); }',
    ],
)
def test_category_words_do_not_prove_a_boundary_or_a_retry(source: str) -> None:
    """Catches message heuristics laundering local state or unproven recovery."""
    sites = scan_rust_source(source, path=PATH)
    assert len(sites) == 1
    assert classify(sites[0]).category == DEBT


@pytest.mark.parametrize("category", [SECURITY, INPUT, RETRIED, DEBT])
def test_reviewed_categories_share_the_same_site_ratchet(category: str) -> None:
    """Catches a Rust-only exception to the reviewed occurrence ceiling."""
    fixtures = {
        SECURITY: "fn ingress() { if !signature_verified { return Err(Denied); } }",
        INPUT: "fn caller_input() { if input.is_empty() { return Err(Invalid); } }",
        RETRIED: "fn observe_once() { Err(Unknown) } fn bounded_owner() { for attempt in 0..3 { if observe_once().is_ok() { break; } } }",
        DEBT: "fn stored() { if !receipt.exists() { return Err(Missing); } }",
    }
    sites = scan_rust_source(fixtures[category], path=PATH)
    document = {
        "fail_closed": [{"category": category, "sites": [[*sites[0].identity, 1]]}],
        "scope": {"audited_paths": []},
        "debt_ceiling": {"total": 0, "unaudited": int(category == DEBT)},
        "operator_waits": [],
        "max_debt": 0,
        "categorized_raises": {"grandfathered": {}, "ceiling": 0},
    }
    assert evaluate_raise_gate(sites, document) == []
    assert evaluate_raise_gate(sites * 2, document)
    assert evaluate_raise_gate([], document)
    lowered = write_counts(document, [], [])
    assert lowered["fail_closed"] == []
    assert evaluate_raise_gate(sites, lowered)


def test_comments_literals_lifetimes_and_nested_token_trees() -> None:
    """Catches regex scans counting commented or quoted refusals as real code."""
    source = """
/* outer /* bail!("nested"); */ Err(Denied) */
fn execute<'a>(request: &'a str) {
    let quoted = br###"Err(Denied) bail!("no")"###;
    let escaped = "\\\" return Err(Denied)";
    let character = '}';
    // std::process::exit(1);
    if rejected { return Result::Err(Error::Denied(format!("bad {}", request))); }
    anyhow::bail! { "refused" };
    std::process::exit(2);
}
"""
    sites = scan_rust_source(source, path=PATH)
    assert [site.exception_class for site in sites] == ["Err", "bail!", "process::exit"]
    assert {site.function for site in sites} == {"execute"}
    assert sites[0].line == 8


def test_test_only_items_are_not_product_refusals() -> None:
    source = """
#[cfg(test)] mod tests { fn fixture() { Err(Denied) } }
#[test] fn unit() { bail!("fixture"); }
#[tokio::test] async fn asynchronous() { Err(Denied) }
fn product() { Err(Denied) }
"""
    assert [s.function for s in scan_rust_source(source, path=PATH)] == ["product"]


def test_distinct_endings_and_new_files_cannot_hide_in_existing_family() -> None:
    sites = scan_rust_source("fn end() { Err(First); Err(Second) }", path=PATH)
    families = propose_families(sites, {"fail_closed": []})
    recorded = families[0]["sites"]
    assert isinstance(recorded, list)
    assert len(recorded) == 2
    other = scan_rust_source("fn end() { Err(First) }", path=PATH + ".rs")
    assert propose_families(other, {"fail_closed": families})


@pytest.mark.parametrize("source", ["fn f() {", "/* unclosed", 'r##"unclosed'])
def test_malformed_scanner_input_cannot_silently_erase_inventory(source: str) -> None:
    with pytest.raises(ValueError):
        scan_rust_source(source, path=PATH)


def test_multiline_literals_preserve_source_lines() -> None:
    assert tokenize('r"first\nsecond"\nErr(x)')[1].line == 3


def test_patterns_are_observations_and_turbofish_and_aliases_are_endings() -> None:
    """Catches counting match/if-let patterns, or missing qualified constructors."""
    source = """
use std::process::exit as terminate;
use anyhow::bail as refuse;
fn observe() {
    if let Err(error) = result { log(error); }
    match result { Err(error) if retryable(error) => observe(), Err(_) => (), _ => () }
    let Err(error) = result else { return; };
    Result::Err::<(), Failure>(Denied);
    refuse!("denied");
    terminate(1);
}
"""
    assert [s.exception_class for s in scan_rust_source(source, path=PATH)] == [
        "Err",
        "bail!",
        "process::exit",
    ]


def test_relocated_rust_debt_uses_rust_tokens_not_python_ast() -> None:
    """A package move must carry the exact Rust ending rather than losing debt."""
    from .blocker_boundaries import relocate_document

    source = "fn end() { Err(Missing) }"
    site = scan_rust_source(source, path=PATH)[0]
    destination = "rust/crates/vonk-agent/src/moved.rs"

    class Move:
        def function(self, path, function, matches_site):
            assert matches_site(source, function)
            return destination

        def scope(self, path, function):
            return function

    document = {"fail_closed": [{"sites": [[*site.identity, 1]]}]}
    moved = relocate_document(document, Move())
    assert moved["fail_closed"][0]["sites"] == [[destination, *site.identity[1:], 1]]


def test_process_failure_codes_are_endings_but_task_abort_is_not() -> None:
    source = "fn main() { task.abort(); std::process::exit(0); ExitCode::from(0); ExitCode::SUCCESS; ExitCode::FAILURE; ExitCode::from(status); }"
    sites = scan_rust_source(source, path=PATH)
    assert [s.exception_class for s in sites] == ["ExitCode::FAILURE", "ExitCode::from"]


def test_file_changes_invalidate_the_inventory_cache(tmp_path, monkeypatch) -> None:
    """A subsequent ending cannot disappear behind a prior cached file parse."""
    from . import blocker_rust

    monkeypatch.setattr(blocker_rust, "REPO_ROOT", tmp_path)
    roots = []
    for crate in blocker_rust.RUST_CRATES:
        root = tmp_path / "rust" / "crates" / crate / "src"
        root.mkdir(parents=True)
        (root / "main.rs").write_text("fn end() { Err(Missing) }")
        roots.append(root)
    assert len(blocker_rust.scan_rust_raises(tuple(roots))) == len(roots)
    (roots[-1] / "main.rs").write_text("fn end() { Err(Missing); Err(Another) }")
    assert len(blocker_rust.scan_rust_raises(tuple(roots))) == len(roots) + 1


def test_external_test_modules_are_excluded_by_cfg_not_only_filename(
    tmp_path, monkeypatch
) -> None:
    from . import blocker_rust

    monkeypatch.setattr(blocker_rust, "REPO_ROOT", tmp_path)
    root = tmp_path / "src"
    root.mkdir()
    (root / "main.rs").write_text(
        "#[cfg(test)] mod fixture; fn product() { Err(Missing) }"
    )
    (root / "fixture.rs").write_text("fn helper() { Err(TestOnly) }")
    sites = blocker_rust.scan_rust_raises((root,))
    assert [site.function for site in sites] == ["product"]
