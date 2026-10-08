"""Regression seams: moving identical content cannot mint or discard allowances."""

import json
from collections import Counter
from pathlib import Path

import pytest

from . import blocker_boundaries as blockers
from . import principle_guards as principles
from . import untyped_mapping_boundaries as mappings
from . import vocabulary_literals as vocabulary
from .package_moves import PackageMoves, identities, record_identities

OLD = "control/src/vonk_control/sample.py"
NEW = "control/src/vonk_control/sample/worker.py"
SOURCE = "def work(value: dict[str, object]):\n    event.wait()\n    return 'waiting-for-operator'\n"


def moved_tree(tmp_path: Path, source: str = SOURCE) -> PackageMoves:
    target = tmp_path / NEW
    target.parent.mkdir(parents=True)
    target.write_text(source)
    return PackageMoves(tmp_path, {OLD: identities(SOURCE)})


@pytest.mark.parametrize("mode", ["waits", "remedies", "reads"])
def test_site_registry_move_preserves_budget_and_rejects_new_site(tmp_path, mode):
    sources = {
        "waits": "def work():\n    event.wait()\n",
        "remedies": "def work():\n    return 'Prepare cache'\n",
        "reads": "@router.get('/x')\ndef work():\n    raise HTTPException(status_code=503)\n",
    }
    source = sources[mode]
    moves = moved_tree(tmp_path, source)
    moves.recorded = {OLD: identities(source)}
    old_sites = principles.scan_source(source, path=OLD, mode=mode)
    assert old_sites
    document = {
        "debt": [
            {"path": OLD, "function": s.function, "kind": s.kind, "count": 1}
            for s in old_sites
        ],
        "exceptions": [],
    }
    relocated = principles.relocate(document, moves)
    sites = principles.scan_source(source, path=NEW, mode=mode)
    assert principles.evaluate_gate(sites, relocated) == []
    assert principles.lower(relocated, sites)["debt"] == relocated["debt"]
    extra = principles.scan_source(
        source.replace("work", "new_work"), path=NEW, mode=mode
    )
    assert principles.evaluate_gate([*sites, *extra], relocated)
    with pytest.raises(ValueError, match="increase debt"):
        principles.lower(relocated, [*sites, *extra])


def test_per_file_registries_split_counts_without_new_allowances(tmp_path):
    moves = moved_tree(tmp_path)
    other = "control/src/vonk_control/sample/other.py"
    source = "def second():\n    return 'waiting-for-operator'\n"
    (tmp_path / other).write_text(source)
    moves.recorded = {OLD: identities(SOURCE + source)}
    counts = Counter({("distinctive", NEW): 1, ("distinctive", other): 1})
    old = {"distinctive": {OLD: 2}}
    relocated = vocabulary.relocated_baseline(counts, old, moves)
    assert relocated["distinctive"] == {NEW: 1, other: 1}
    assert vocabulary.problems(counts, relocated) == []
    counts[("distinctive", NEW)] += 1
    assert vocabulary.problems(counts, relocated)
    assert (
        sum(vocabulary.lowered_baseline(counts, relocated)["distinctive"].values()) == 2
    )

    sites = mappings.scan_source(SOURCE, path=NEW)
    for group in ("debt", "permanent"):
        entry = {"path": OLD, "count": 1}
        if group == "permanent":
            entry.update(
                function="work",
                annotation="dict[str, object]",
                reason="External data utility",
            )
        allowlist = {"debt": [], "permanent": [], group: [entry]}
        relocated = mappings.relocated_allowlist(sites, allowlist, moves)
        assert relocated[group][0]["path"] == NEW
        assert mappings.evaluate_gate(sites, relocated) == []
        assert mappings.evaluate_gate([*sites, *sites], relocated)


def test_blocker_sites_loops_and_both_call_edge_ends_move(tmp_path, monkeypatch):
    moves = moved_tree(tmp_path)
    document = {
        "operator_waits": [
            {
                "path": OLD,
                "function": "work",
                "kind": blockers.CONTROL_STATE_ASSIGNMENT,
                "sites": 1,
                "verdict": "REMOVE",
            }
        ],
        "max_debt": 1,
        "fail_closed": [
            {
                "family": "sample",
                "category": "bookkeeping-debt",
                "sites": [[OLD, "SampleError", "work", "", 1]],
            }
        ],
        "debt_ceiling": {"total": 1, "unaudited": 0},
        "scope": {"audited_paths": [OLD], "guard_paths": [OLD]},
        "categorized_raises": {"grandfathered": {}, "ceiling": 0},
        "retry_loops": [{"path": OLD, "function": "work", "catches": ["SampleError"]}],
        "call_edges": [
            {
                "path": OLD,
                "function": "work",
                "calls": [{"path": OLD, "function": "work"}],
            }
        ],
    }
    relocated = blockers.relocate_document(document, moves)
    assert relocated["retry_loops"][0]["path"] == NEW
    assert relocated["call_edges"][0]["path"] == NEW
    assert relocated["call_edges"][0]["calls"][0]["path"] == NEW
    waits = [blockers.WaitSite(NEW, "work", blockers.CONTROL_STATE_ASSIGNMENT, 2)]
    raises = [blockers.RaiseSite(NEW, "SampleError", "work", "", 2)]
    assert blockers.evaluate_wait_gate(waits, relocated) == []
    assert blockers.evaluate_raise_gate(raises, relocated) == []
    original = blockers.relocate_document
    monkeypatch.setattr(blockers, "relocate_document", lambda doc: original(doc, moves))
    assert blockers.write_counts(document, waits, raises) == relocated
    assert blockers.evaluate_raise_gate([*raises, *raises], relocated)
    assert document["fail_closed"][0]["sites"][0][0] == OLD


@pytest.mark.parametrize(
    "change", ["body", "copy", "outside", "missing-source", "original-exists"]
)
def test_new_changed_copied_or_unproven_content_gets_no_credit(tmp_path, change):
    moves = moved_tree(tmp_path)
    if change == "body":
        (tmp_path / NEW).write_text(
            SOURCE.replace("event.wait()", "event.wait(); event.wait()")
        )
    elif change == "copy":
        (tmp_path / NEW).with_name("copy.py").write_text(SOURCE)
    elif change == "outside":
        (tmp_path / NEW).unlink()
        (tmp_path / "unrelated.py").write_text(SOURCE)
    elif change == "missing-source":
        moves.recorded = {}
    else:
        (tmp_path / OLD).write_text(SOURCE)
    assert moves.function(OLD, "work") == OLD
    assert moves.counts({OLD: 1}, {NEW: 1}).get(NEW, 0) == 0


def test_untyped_writer_moves_entries_and_refuses_new_debt(tmp_path, monkeypatch):
    moves = moved_tree(tmp_path)
    path = tmp_path / "allowlist.json"
    path.write_text(
        json.dumps(
            {
                "schema": 1,
                "permanent": [],
                "debt": [{"path": OLD, "count": 1}],
                "content_identities": {OLD: identities(SOURCE)},
            }
        )
    )
    sites = mappings.scan_source(SOURCE, path=NEW)
    monkeypatch.setattr(mappings, "scan_sites", lambda: sites)
    original = mappings.relocated_allowlist
    monkeypatch.setattr(
        mappings, "relocated_allowlist", lambda sites, doc: original(sites, doc, moves)
    )
    assert mappings.update_debt(path) == 1
    assert json.loads(path.read_text())["debt"] == [{"path": NEW, "count": 1}]
    written = path.read_text()
    sites.extend(mappings.scan_source(SOURCE.replace("work", "new_work"), path=NEW))
    with pytest.raises(ValueError, match="increase debt"):
        mappings.update_debt(path)
    assert path.read_text() == written


def test_normalization_ignores_positions_comments_and_import_location(tmp_path):
    moved = "from .dependency import event\n\n" + SOURCE.replace(
        "    event.wait()", "    event.wait()  # unchanged call"
    )
    moves = moved_tree(tmp_path, moved)
    assert moves.function(OLD, "work") == NEW
    assert moves.counts({OLD: 1}, {NEW: 1}) == {NEW: 1}


def test_registry_never_credits_same_name_with_a_changed_body(tmp_path):
    moves = moved_tree(tmp_path, SOURCE.replace("event.wait()", "other.wait()"))
    entry = {"path": OLD, "function": "work", "kind": "wait", "count": 1}
    old = {"debt": [entry], "exceptions": []}
    current = {"debt": [{**entry, "path": NEW}], "exceptions": []}
    assert principles.history_gate(current, old, moves)


@pytest.mark.parametrize("mode", ["waits", "remedies", "reads"])
def test_undigested_sites_match_only_unique_recorded_kind(tmp_path, mode):
    source = {
        "waits": "def work():\n    event.wait()\n",
        "remedies": "def work():\n    return 'Prepare cache'\n",
        "reads": "@router.get('/x')\ndef work():\n    raise HTTPException(status_code=503)\n",
    }[mode]
    moves = moved_tree(tmp_path, source)
    moves.recorded = {}
    site = principles.scan_source(source, path=OLD, mode=mode)[0]
    document = {
        "debt": [
            {"path": OLD, "function": site.function, "kind": site.kind, "count": 1}
        ],
        "exceptions": [],
    }
    assert principles.relocate(document, moves)["debt"][0]["path"] == NEW
    (tmp_path / NEW).with_name("copy.py").write_text(source)
    assert principles.relocate(document, moves)["debt"][0]["path"] == OLD
    (tmp_path / NEW).with_name("copy.py").unlink()
    (tmp_path / NEW).write_text("def work():\n    return 1\n")
    assert principles.relocate(document, moves)["debt"][0]["path"] == OLD


def test_writer_snapshot_survives_removal_and_rejects_changed_body(tmp_path):
    old = tmp_path / OLD
    old.parent.mkdir(parents=True)
    old.write_text(SOURCE)
    document = record_identities({"debt": [{"path": OLD, "count": 1}]}, tmp_path)
    old.unlink()
    new = tmp_path / NEW
    new.parent.mkdir()
    new.write_text(SOURCE)
    moves = PackageMoves(tmp_path, document["content_identities"])
    assert moves.counts({OLD: 1}, {NEW: 1}) == {NEW: 1}
    new.write_text(SOURCE.replace("event.wait()", "other.wait()"))
    moves = PackageMoves(tmp_path, document["content_identities"])
    assert moves.counts({OLD: 1}, {NEW: 1}) == {OLD: 1}


def test_path_only_budget_without_identity_never_moves(tmp_path):
    moves = moved_tree(tmp_path)
    moves.recorded = {}
    assert moves.counts({OLD: 1}, {NEW: 1}) == {OLD: 1}


def test_undigested_raise_matches_exception_and_reason(tmp_path):
    source = "def work():\n    raise SampleError('sample.reason')\n"
    moves = moved_tree(tmp_path, source)
    moves.recorded = {}
    document = {
        "fail_closed": [{"sites": [[OLD, "SampleError", "work", "sample.reason", 1]]}]
    }
    assert (
        blockers.relocate_document(document, moves)["fail_closed"][0]["sites"][0][0]
        == NEW
    )
    for changed in (
        source.replace("SampleError", "OtherError"),
        source.replace("sample.reason", "other.reason"),
    ):
        (tmp_path / NEW).write_text(changed)
        assert (
            blockers.relocate_document(document, moves)["fail_closed"][0]["sites"][0][0]
            == OLD
        )


@pytest.mark.parametrize("writer", ["mappings", "vocabulary", "blockers", "principles"])
def test_registry_writers_persist_identities_used_after_split(
    tmp_path, monkeypatch, writer
):
    old = tmp_path / OLD
    old.parent.mkdir(parents=True)
    old.write_text(SOURCE)
    path = tmp_path / "registry.json"
    if writer == "mappings":
        monkeypatch.setattr(mappings, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(
            mappings, "scan_sites", lambda: mappings.scan_source(SOURCE, path=OLD)
        )
        path.write_text(
            json.dumps(
                {"schema": 1, "debt": [{"path": OLD, "count": 1}], "permanent": []}
            )
        )
        mappings.update_debt(path)
        document = json.loads(path.read_text())
    elif writer == "vocabulary":
        monkeypatch.setattr(vocabulary, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(vocabulary, "BASELINE_PATH", path)
        path.write_text(json.dumps({"schema": 1, "distinctive": {OLD: 1}}))
        vocabulary.write_baseline(Counter({("distinctive", OLD): 1}))
        document = json.loads(path.read_text())
    elif writer == "blockers":
        monkeypatch.setattr(blockers, "REPO_ROOT", tmp_path)
        document = json.loads(
            blockers.dump_document(
                {
                    "operator_waits": [{"path": OLD, "function": "work", "sites": 1}],
                    "fail_closed": [],
                },
                record_content=True,
            )
        )
    else:
        monkeypatch.setattr(principles, "ROOT", tmp_path)
        sites = principles.scan_source(SOURCE, path=OLD, mode="waits")
        document = principles.lower(
            {
                "debt": [
                    {
                        "path": OLD,
                        "function": site.function,
                        "kind": site.kind,
                        "count": 1,
                    }
                    for site in sites
                ],
                "exceptions": [],
            },
            sites,
        )
    old.unlink()
    new = tmp_path / NEW
    new.parent.mkdir()
    new.write_text(SOURCE)
    assert (
        PackageMoves(tmp_path, document["content_identities"]).function(OLD, "work")
        == NEW
    )


def test_invalid_recorded_identity_is_not_silently_ignored(tmp_path):
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        PackageMoves(tmp_path, {OLD: {"work": 123}})


def test_undigested_retry_registration_keeps_its_exception_identity(tmp_path):
    source = "def work():\n    try:\n        operation()\n    except SampleError:\n        return\n"
    moves = moved_tree(tmp_path, source)
    moves.recorded = {}
    document = {
        "retry_loops": [{"path": OLD, "function": "work", "catches": ["SampleError"]}],
        "call_edges": [
            {
                "path": OLD,
                "function": "work",
                "calls": [{"path": OLD, "function": "work"}],
            }
        ],
        "fail_closed": [],
    }
    relocated = blockers.relocate_document(document, moves)
    assert relocated["retry_loops"][0]["path"] == NEW
    assert relocated["call_edges"][0]["calls"][0]["path"] == NEW
    (tmp_path / NEW).write_text(source.replace("SampleError", "OtherError"))
    assert blockers.relocate_document(document, moves)["retry_loops"][0]["path"] == OLD


def test_rust_split_carries_only_unique_matching_findings(tmp_path: Path) -> None:
    from .principle_guards import scan_rust

    old = "rust/operations.rs"
    package = tmp_path / "rust/operations"
    package.mkdir(parents=True)
    target = package / "storage.rs"
    target.write_text("fn collect() { loop { read(); } }")
    document = {
        "schema": 1,
        "debt": [
            {
                "path": old,
                "function": "collect",
                "kind": "rust-loop-without-deadline",
                "count": 1,
            }
        ],
        "exceptions": [],
    }
    moves = PackageMoves(tmp_path)
    current = {
        **document,
        "debt": [{**document["debt"][0], "path": "rust/operations/storage.rs"}],
    }
    assert (
        principles.evaluate_gate(
            scan_rust(target.read_text(), path="rust/operations/storage.rs"),
            principles.relocate(document, moves),
        )
        == []
    )
    assert principles.history_gate(current, document, moves) == []
    # Two methods with the same name cannot share the old allowance.
    (package / "other.rs").write_text(target.read_text())
    assert principles.history_gate(current, document, PackageMoves(tmp_path))


@pytest.mark.parametrize("old", ["rust/src/lib.rs", "rust/operations.rs"])
def test_rust_facade_split_carries_findings_without_pooling_debt(tmp_path, old):
    """A retained facade must neither mint debt nor hide a duplicate owner."""
    facade = tmp_path / old
    facade.parent.mkdir(parents=True)
    package = facade.parent if facade.name == "lib.rs" else facade.with_suffix("")
    package.mkdir(exist_ok=True)
    facade.write_text("mod storage;\n")
    target = package / "storage.rs"
    target.write_text("fn collect() { loop { read(); } }")
    entry = {
        "path": old,
        "function": "collect",
        "kind": "rust-loop-without-deadline",
        "count": 1,
    }
    previous = {"debt": [entry], "exceptions": []}
    current = {
        "debt": [{**entry, "path": target.relative_to(tmp_path).as_posix()}],
        "exceptions": [],
    }
    # An unrelated sibling is not part of the facade's module tree.
    (package / "unrelated.rs").write_text(target.read_text())
    assert principles.history_gate(current, previous, PackageMoves(tmp_path)) == []
    # A second registered owner cannot share the old allowance.
    facade.write_text("mod storage;\nmod unrelated;\n")
    assert principles.history_gate(current, previous, PackageMoves(tmp_path))
    # A function still present at its original owner is not a move.
    facade.write_text("mod storage;\n" + target.read_text())
    assert principles.history_gate(current, previous, PackageMoves(tmp_path))
