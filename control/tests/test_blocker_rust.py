"""Scanner-output and bounded source-observation regression fixtures."""

import pytest

from .blocker_rust import rust_identities, scan_rust_source, tokenize

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
def test_error_words_do_not_change_scanner_coverage(source: str) -> None:
    """All explicit ending forms remain visible regardless of diagnostic words."""
    sites = scan_rust_source(source, path=PATH)
    assert len(sites) == 1
    assert sites[0].path == PATH


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


def test_behavior_identity_survives_moves_and_invalidates_changed_recovery():
    source = "fn end() { retry(observe); Err(Missing) }"
    assert rust_identities(source) == rust_identities("// moved\n" + source)
    assert rust_identities(source) != rust_identities("fn end() { Err(Missing) }")


@pytest.mark.parametrize("source", ["fn f() {", "/* unclosed", 'r##"unclosed'])
def test_malformed_scanner_input_cannot_silently_erase_inventory(source: str) -> None:
    published = None
    try:
        published = scan_rust_source(source, path=PATH)
    except ValueError:
        pass
    assert published is None
    assert scan_rust_source("fn fresh() { Err(Observed) }", path=PATH)


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


def test_relocated_rust_ending_uses_real_package_discovery(tmp_path) -> None:
    """An unchanged function moved to a module retains its inventory identity."""
    from .blocker_boundaries import relocate_document
    from .package_moves import PackageMoves, record_identities

    source = "fn end() { Err(Missing) }"
    original = tmp_path / PATH
    original.parent.mkdir(parents=True)
    original.write_text(source)
    site = scan_rust_source(source, path=PATH)[0]
    document = record_identities(
        {"fail_closed": [{"sites": [[*site.identity, 1]]}]}, tmp_path
    )
    package = original.with_suffix("")
    package.mkdir()
    destination = package / "moved.rs"
    original.rename(destination)
    destination_path = destination.relative_to(tmp_path).as_posix()
    moved = relocate_document(
        document, PackageMoves(tmp_path, document["content_identities"])
    )
    derived = scan_rust_source(destination.read_text(), path=destination_path)
    assert moved["fail_closed"][0]["sites"] == [[*derived[0].identity, 1]]
    destination.write_text("fn end() { perform_new_effect(); Err(Missing) }")
    changed = relocate_document(
        document, PackageMoves(tmp_path, document["content_identities"])
    )
    assert changed["fail_closed"][0]["sites"] == [[*site.identity, 1]]


def test_process_failure_codes_are_endings_but_task_abort_is_not() -> None:
    source = "fn main() { task.abort(); std::process::exit(0); ExitCode::from(0); ExitCode::SUCCESS; ExitCode::FAILURE; ExitCode::from(status); }"
    sites = scan_rust_source(source, path=PATH)
    assert [s.exception_class for s in sites] == ["ExitCode::FAILURE", "ExitCode::from"]


def test_file_changes_are_observed_on_the_next_scan(tmp_path, monkeypatch) -> None:
    """A subsequent ending cannot disappear behind an earlier file observation."""
    from . import blocker_rust

    monkeypatch.setattr(blocker_rust, "REPO_ROOT", tmp_path)
    roots = []
    for crate in blocker_rust.RUST_CRATES:
        root = tmp_path / "rust" / "crates" / crate / "src"
        root.mkdir(parents=True)
        (root.parent / "Cargo.toml").write_text('[package]\nname="fixture"\n')
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
    (root.parent / "Cargo.toml").write_text('[package]\nname="fixture"\n')
    (root / "main.rs").write_text(
        "#[cfg(test)] mod fixture; fn product() { Err(Missing) }"
    )
    (root / "fixture.rs").write_text("fn helper() { Err(TestOnly) }")
    sites = blocker_rust.scan_rust_raises((root,))
    assert [site.function for site in sites] == ["product"]


def test_closure_tails_construct_errors_while_alternative_patterns_do_not() -> None:
    """Closure delimiters must not be confused with pattern alternatives."""
    source = """
fn observe() {
    let empty = || Err(Empty);
    let argument = |arg| Err(arg);
    match value { Err(First) | Err(Second) => (), _ => () }
}
"""
    sites = scan_rust_source(source, path=PATH)
    assert [(site.function, site.code, site.line) for site in sites] == [
        ("observe", "Empty", 3),
        ("observe", "arg", 4),
    ]


def test_module_membership_overrides_names_and_resolves_visibility_and_paths(
    tmp_path, monkeypatch
) -> None:
    """Production tests/test_support modules count; attributed test modules do not."""
    from . import blocker_rust

    monkeypatch.setattr(blocker_rust, "REPO_ROOT", tmp_path)
    root = tmp_path / "src"
    root.mkdir()
    (root.parent / "Cargo.toml").write_text('[package]\nname="fixture"\n')
    (root / "main.rs").write_text(
        "mod tests;\n"
        "pub(crate) mod test_support;\n"
        '#[cfg(test)] #[allow(dead_code)] #[path = "fixture.rs"] pub mod fixture;\n'
        '#[path = "live.rs"] pub mod renamed;\n'
    )
    (root / "tests.rs").write_text("fn production_named_tests() { Err(One) }")
    support = root / "test_support"
    support.mkdir()
    (support / "mod.rs").write_text("fn production_support() { Err(Two) }")
    (root / "fixture.rs").write_text("fn fixture() { Err(TestOnly) }")
    (root / "live.rs").write_text("fn renamed() { Err(Three) }")
    # Unreferenced files do not establish module membership either.
    (root / "orphan.rs").write_text("fn orphan() { Err(Unreachable) }")
    sites = blocker_rust.scan_rust_raises((root,))
    assert {site.function for site in sites} == {
        "production_named_tests",
        "production_support",
        "renamed",
    }


def test_inline_module_resolves_external_production_child(
    tmp_path, monkeypatch
) -> None:
    from . import blocker_rust

    monkeypatch.setattr(blocker_rust, "REPO_ROOT", tmp_path)
    root = tmp_path / "src"
    (root / "outer").mkdir(parents=True)
    (root.parent / "Cargo.toml").write_text('[package]\nname="fixture"\n')
    (root / "main.rs").write_text("mod outer { pub mod child; }")
    (root / "outer" / "child.rs").write_text("fn child() { Err(Observed) }")
    assert [site.function for site in blocker_rust.scan_rust_raises((root,))] == [
        "child"
    ]


def test_incomplete_module_observation_rereads_then_admits_fresh_scan(
    tmp_path, monkeypatch
) -> None:
    """Missing/damaged source cannot become zero debt or poison the next scan."""
    from vonk_agent_protocol import UnknownOutcomeError

    from . import blocker_rust

    monkeypatch.setattr(blocker_rust, "REPO_ROOT", tmp_path)
    root = tmp_path / "src"
    root.mkdir()
    (root.parent / "Cargo.toml").write_text('[package]\nname="fixture"\n')
    (root / "main.rs").write_text("mod child;")
    child = root / "child.rs"
    original_read = type(child).read_text
    reads = []

    def read(path, *args, **kwargs):
        reads.append(path)
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(type(child), "read_text", read)
    for damaged in (
        None,
        "fn child() {",
        '#[cfg_attr(test, path = "other.rs")] mod next;',
    ):
        if damaged is not None:
            child.write_text(damaged)
        reads.clear()
        published = None
        try:
            published = blocker_rust.scan_rust_raises((root,))
        except UnknownOutcomeError:
            pass
        assert published is None
        assert reads.count(root / "main.rs") == 3
        child.write_text("fn child() { Err(Observed) }")
        fresh = blocker_rust.scan_rust_raises((root,))
        assert [(site.function, site.code) for site in fresh] == [("child", "Observed")]


def test_transient_read_loss_recovers_inside_the_observation_budget(
    tmp_path, monkeypatch
) -> None:
    from . import blocker_rust

    monkeypatch.setattr(blocker_rust, "REPO_ROOT", tmp_path)
    root = tmp_path / "src"
    root.mkdir()
    (root.parent / "Cargo.toml").write_text('[package]\nname="fixture"\n')
    entry = root / "main.rs"
    entry.write_text("fn execute() { Err(Observed) }")
    original_read = type(entry).read_text
    calls = 0

    def read(path, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise FileNotFoundError(path)
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(type(entry), "read_text", read)
    sites = blocker_rust.scan_rust_raises((root,))
    assert [(site.function, site.code) for site in sites] == [("execute", "Observed")]
    assert calls == 2
    assert blocker_rust.scan_rust_raises((root,)) == sites


def test_cargo_declared_binary_and_automatic_binary_are_product_roots(
    tmp_path, monkeypatch
) -> None:
    from . import blocker_rust

    monkeypatch.setattr(blocker_rust, "REPO_ROOT", tmp_path)
    root = tmp_path / "src"
    (root / "bin").mkdir(parents=True)
    (tmp_path / "Cargo.toml").write_text(
        '[package]\nname = "fixture"\n[[bin]]\nname = "probe"\npath = "src/probe.rs"\n'
    )
    (root / "main.rs").write_text("fn main() { Err(Main) }")
    (root / "probe.rs").write_text("mod child; fn probe() { Err(Probe) }")
    (root / "child.rs").write_text("fn child() { Err(Child) }")
    (root / "bin" / "automatic.rs").write_text("fn automatic() { Err(Automatic) }")
    assert {site.function for site in blocker_rust.scan_rust_raises((root,))} == {
        "main",
        "probe",
        "child",
        "automatic",
    }


def test_manifest_loss_never_publishes_partial_combined_scan(tmp_path, monkeypatch):
    from vonk_agent_protocol import UnknownOutcomeError

    from . import blocker_boundaries, blocker_rust

    control = tmp_path / "control/src"
    control.mkdir(parents=True)
    root = tmp_path / "rust/crates/fixture/src"
    root.mkdir(parents=True)
    (root / "main.rs").write_text("fn main() { Err(Main) }")
    (root / "special.rs").write_text("fn special() { Err(Special) }")
    manifest = root.parent / "Cargo.toml"
    body = '[package]\nname="fixture"\n[[bin]]\nname="special"\npath="src/special.rs"\n'
    manifest.write_text(body)
    monkeypatch.setattr(blocker_rust, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(blocker_boundaries, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(blocker_boundaries, "CONTROL_SOURCE_ROOT", control)
    public_rust = blocker_rust.scan_rust_raises
    monkeypatch.setattr(blocker_rust, "scan_rust_raises", lambda: public_rust((root,)))
    combined = lambda: blocker_boundaries.scan_raises(control)
    expected = combined()
    assert {site.function for site in expected} == {"main", "special"}
    manifest.unlink()
    published = None
    try:
        published = combined()
    except UnknownOutcomeError:
        pass
    assert published is None
    manifest.write_text(body)
    assert combined() == expected
    assert combined() == expected


def test_configuration_attributes_includes_and_block_modules_are_observed(
    tmp_path, monkeypatch
):
    from . import blocker_rust

    monkeypatch.setattr(blocker_rust, "REPO_ROOT", tmp_path)
    root = tmp_path / "src"
    root.mkdir()
    (root.parent / "Cargo.toml").write_text('[package]\nname="fixture"\n')
    (root / "main.rs").write_text(
        '#[cfg_attr(feature="logging", allow(dead_code))] fn live() { Err(Live) }\n'
        '#[cfg_attr(feature="alternate", path="alternate.rs")] mod selected;\n'
        'include!("included.rs");\n'
        "fn owner() { mod inside { mod child; } }\n"
    )
    (root / "selected.rs").write_text("fn default_variant() { Err(Default) }")
    (root / "alternate.rs").write_text("fn alternate() { Err(Alternate) }")
    (root / "included.rs").write_text("fn included() { Err(Included) }")
    (root / "inside").mkdir()
    (root / "inside/child.rs").write_text("fn child() { Err(Child) }")
    expected = {"live", "default_variant", "alternate", "included", "child"}
    assert {
        site.function for site in blocker_rust.scan_rust_raises((root,))
    } == expected
    assert {
        site.function for site in blocker_rust.scan_rust_raises((root,))
    } == expected
