from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _module():
    loader = importlib.machinery.SourceFileLoader(
        "lifecycle_counts", str(ROOT / "scripts/lifecycle-counts")
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


WRITERS = {"writers": [{"count": 2}, {"count": 3}]}
BLOCKERS = {
    "max_debt": 4,
    "debt_ceiling": {"total": 10, "unaudited": 7},
    "operator_waits": [
        # One site, listed once per operation kind: counted once.
        {"path": "a.py", "function": "f", "kind": "k", "sites": 2},
        {"path": "a.py", "function": "f", "kind": "k", "sites": 2},
        {"path": "b.py", "function": "g", "kind": "k", "sites": 1},
    ],
    "fail_closed": [
        {"sites": [["a.py", "E", "f", "c", 3], ["a.py", "E", "g", "d", 4]]},
        {"sites": [["b.py", "E", "h", "e", 1]]},
    ],
}
REAL = {"writers": 5, "operator_waits": 3, "raises": 8, "debt": 21}


def test_counts_come_from_the_allowlists() -> None:
    assert _module().count_documents(WRITERS, BLOCKERS) == REAL


def test_the_recorded_counts_equal_what_the_scanners_find() -> None:
    # The allowlists are what the control suite ratchets against the code, so a
    # count read from them is the count of the code.
    module = _module()
    counts = module.counts_from(module.reader_for(None))
    assert set(counts) == set(module.KEYS)
    assert all(value >= 0 for value in counts.values())


def test_only_lifecycle_raise_and_allowlist_files_need_a_report() -> None:
    module = _module()
    assert module.is_covered("tools/blocker-allowlist")
    assert module.is_covered(
        "tools/blocker-allowlist/control/src/vonk_control/step_ca.py.json"
    )
    assert module.is_covered("control/src/vonk_control/lifecycle/job.py")
    assert module.is_covered("rust/crates/vonk-agent/src/executor/mod.rs")
    assert not module.is_covered("control/src/vonk_control/api.py")
    assert not module.is_covered("docs/runbooks/vonkctl.md")


def test_an_uncovered_change_needs_no_report() -> None:
    assert _module().check("", REAL, REAL, ["docs/a.md"]) == []


def test_a_covered_change_without_a_report_fails_with_the_block_to_paste() -> None:
    module = _module()
    messages = module.check("no numbers", REAL, REAL, [module.BLOCKERS])
    assert len(messages) == 2
    assert "before: writers=5 operator_waits=3 raises=8 debt=21" in messages[0]


def test_a_report_must_match_the_real_counts() -> None:
    module = _module()
    after = {**REAL, "writers": 4}
    honest = module.report_block(REAL, after)
    assert module.check(honest, REAL, after, [module.WRITERS]) == []
    lying = module.report_block(REAL, REAL)
    messages = module.check(lying, REAL, after, [module.WRITERS])
    assert len(messages) == 1 and messages[0].startswith("'after:'")


def test_report_lines_parse_in_markdown_bullets_and_any_case() -> None:
    body = (
        "x\n- Before: writers=1 operator_waits=2 raises=3 debt=4\n* AFTER : writers=0\n"
    )
    parsed = _module().parse_report(body)
    assert parsed["before"]["debt"] == 4
    assert parsed["after"] == {"writers": 0}


def test_cli_prints_json_for_the_tree(capsys) -> None:
    module = _module()
    assert module.main([]) == 0
    assert set(json.loads(capsys.readouterr().out)) == set(module.KEYS)


def test_a_commit_without_the_writers_allowlist_counts_zero_writers() -> None:
    module = _module()
    blockers = json.dumps(BLOCKERS)

    def read(path: str) -> str:
        if path == module.WRITERS:
            raise FileNotFoundError(path)
        return blockers

    assert module.counts_from(read)["writers"] == 0


def test_operation_kind_groups_do_not_overwrite_each_other() -> None:
    blockers = {
        **BLOCKERS,
        "operator_waits": [
            {
                "path": "a.rs",
                "function": "execute",
                "kind": "rust-result",
                "sites": 7,
                "operation_kinds": ["build"],
            },
            {
                "path": "a.rs",
                "function": "execute",
                "kind": "rust-result",
                "sites": 7,
                "operation_kinds": ["cleanup"],
            },
            {
                "path": "a.rs",
                "function": "execute",
                "kind": "rust-result",
                "sites": 7,
                "operation_kinds": ["run"],
            },
        ],
    }
    assert _module().count_documents(WRITERS, blockers)["operator_waits"] == 21


def test_incomplete_report_is_a_diagnostic_instead_of_a_key_error() -> None:
    messages = _module().check(
        "before: writers=5\nafter: writers=5", REAL, REAL, ["scripts/lifecycle-counts"]
    )
    assert len(messages) == 2


def test_history_observation_retries_once_and_does_not_poison_a_fresh_read(monkeypatch):
    module = _module()
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if len(calls) <= 2:
            raise subprocess.CalledProcessError(1, command)
        return subprocess.CompletedProcess(command, 0, "observed", "")

    monkeypatch.setattr(module.subprocess, "run", run)
    import pytest

    with pytest.raises(subprocess.CalledProcessError):
        module._git("show", "fixture")
    assert len(calls) == 2
    assert module._git("show", "fixture") == "observed"
    assert len(calls) == 3


@pytest.mark.parametrize("fault", ["json", "process", "structure"])
def test_unreadable_existing_counts_are_not_reported_as_zero_and_fresh_read_recovers(
    fault,
):
    module = _module()
    reads = []

    def read(path):
        if path == module.WRITERS:
            raise FileNotFoundError(path)
        if path == module.BLOCKERS:
            reads.append(path)
            if len(reads) <= 2:
                if fault == "process":
                    raise subprocess.CalledProcessError(1, ["git", "show"])
                return "unreadable" if fault == "json" else "{}"
            return json.dumps(BLOCKERS)
        raise FileNotFoundError(path)

    with pytest.raises(RuntimeError):
        module.counts_from(read)
    assert len(reads) == 2
    assert (
        module.counts_from(read)["debt"]
        == module.count_documents({"writers": []}, BLOCKERS)["debt"]
    )
    assert len(reads) == 3


def test_existing_unreadable_optional_inventory_is_unknown_then_recovers():
    module = _module()
    reads = []

    def read(path):
        if path == module.WRITERS:
            raise FileNotFoundError(path)
        if path == module.BLOCKERS:
            return json.dumps(BLOCKERS)
        if path == module.INVENTORIES["untyped"]:
            reads.append(path)
            if len(reads) <= 2:
                raise subprocess.CalledProcessError(1, ["git", "show"])
            return json.dumps({"debt": [{"count": 4}]})
        raise FileNotFoundError(path)

    with pytest.raises(RuntimeError):
        module.counts_from(read)
    assert len(reads) == 2
    assert module.counts_from(read)["untyped"] == 4
    assert len(reads) == 3
