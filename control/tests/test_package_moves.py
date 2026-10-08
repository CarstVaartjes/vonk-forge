"""Regression seams: moving identical content cannot mint or discard allowances."""

import json
from collections import Counter
from pathlib import Path

import pytest

from . import blocker_boundaries as blockers
from . import principle_guards as principles
from . import untyped_mapping_boundaries as mappings
from . import vocabulary_literals as vocabulary
from .package_moves import PackageMoves

OLD = "control/src/vonk_control/sample.py"
NEW = "control/src/vonk_control/sample/worker.py"
SOURCE = "def work(value: dict[str, object]):\n    event.wait()\n    return 'waiting-for-operator'\n"


def moved_tree(tmp_path: Path, source: str = SOURCE) -> PackageMoves:
    target = tmp_path / NEW
    target.parent.mkdir(parents=True)
    target.write_text(source)
    return PackageMoves(tmp_path, lambda path: SOURCE if path == OLD else None)


@pytest.mark.parametrize("mode", ["waits", "remedies", "reads"])
def test_site_registry_move_preserves_budget_and_rejects_new_site(tmp_path, mode):
    sources = {
        "waits": "def work():\n    event.wait()\n",
        "remedies": "def work():\n    return 'Prepare cache'\n",
        "reads": "@router.get('/x')\ndef work():\n    raise HTTPException(status_code=503)\n",
    }
    source = sources[mode]
    moves = moved_tree(tmp_path, source)
    moves.source = lambda path: source
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
    assert principles.lower(relocated, sites) == relocated
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
    moves.source = lambda path: SOURCE + source
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
        moves.source = lambda path: None
    else:
        (tmp_path / OLD).write_text(SOURCE)
    assert moves.function(OLD, "work") == OLD
    assert moves.counts({OLD: 1}, {NEW: 1}).get(NEW, 0) == 0


def test_untyped_writer_moves_entries_and_refuses_new_debt(tmp_path, monkeypatch):
    moves = moved_tree(tmp_path)
    path = tmp_path / "allowlist.json"
    path.write_text(
        json.dumps({"schema": 1, "permanent": [], "debt": [{"path": OLD, "count": 1}]})
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


def test_history_never_credits_same_name_with_a_changed_body(tmp_path):
    moves = moved_tree(tmp_path, SOURCE.replace("event.wait()", "other.wait()"))
    entry = {"path": OLD, "function": "work", "kind": "wait", "count": 1}
    old = {"debt": [entry], "exceptions": []}
    current = {"debt": [{**entry, "path": NEW}], "exceptions": []}
    assert principles.history_gate(current, old, moves)
