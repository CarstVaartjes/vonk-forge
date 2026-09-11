"""Pin the type-ratchet rule: a reviewed allowlist, not a per-file budget.

The point of the list is that an unlisted error fails even when the file's total
count is unchanged, and that a listed error which stops occurring must be
removed. These tests exercise the decision directly so the gate cannot quietly
degrade into a counter.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/check-python-types"


def _module() -> ModuleType:
    loader = importlib.machinery.SourceFileLoader("check_python_types", str(SCRIPT))
    specification = importlib.util.spec_from_loader(loader.name, loader)
    assert specification is not None
    module = importlib.util.module_from_spec(specification)
    loader.exec_module(module)
    return module


def _exception(**overrides: object) -> dict[str, object]:
    entry: dict[str, object] = {
        "file": "control/tests/example.py",
        "rule": "reportArgumentType",
        "count": 1,
        "reason": "The case asserts the runtime refusal of an untyped value.",
    }
    entry.update(overrides)
    return entry


def test_a_reviewed_exception_with_a_reason_is_accepted() -> None:
    module = _module()
    key = ("control/tests/example.py", "reportArgumentType")

    assert module.evaluate({key: 1}, [_exception()]) == []


def test_an_unlisted_error_fails_even_at_the_same_total() -> None:
    module = _module()
    listed = ("control/tests/example.py", "reportArgumentType")
    hidden = ("control/tests/example.py", "reportReturnType")

    problems = module.evaluate({listed: 1, hidden: 1}, [_exception()])

    assert problems == [
        "unlisted type error: control/tests/example.py reportReturnType (1)"
    ]


def test_a_second_error_of_the_same_rule_in_the_same_file_fails() -> None:
    module = _module()
    key = ("control/tests/example.py", "reportArgumentType")

    problems = module.evaluate({key: 2}, [_exception()])

    assert problems == [
        "type errors increased: control/tests/example.py reportArgumentType: 1 -> 2"
    ]


def test_an_exception_that_no_longer_occurs_is_stale() -> None:
    module = _module()

    problems = module.evaluate({}, [_exception()])

    assert problems == [
        (
            "stale exception (remove it with --update): "
            "control/tests/example.py reportArgumentType: 1 -> 0"
        )
    ]


def test_an_exception_without_a_reason_fails() -> None:
    module = _module()
    key = ("control/tests/example.py", "reportArgumentType")

    problems = module.evaluate({key: 1}, [_exception(reason="")])

    assert problems == [
        "exception has no reason: control/tests/example.py reportArgumentType"
    ]


def test_a_duplicate_exception_fails() -> None:
    module = _module()
    key = ("control/tests/example.py", "reportArgumentType")

    problems = module.evaluate({key: 1}, [_exception(), _exception()])

    assert problems == [
        "duplicate exception: control/tests/example.py reportArgumentType"
    ]


def test_update_keeps_known_reasons_and_reports_new_ones(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _module()
    target = tmp_path / "baseline.json"
    monkeypatch.setattr(module, "BASELINE", target)
    known = _exception()
    added = (
        "control/src/example.py",
        "reportGeneralTypeIssues",
    )

    module._write(
        {
            ("control/tests/example.py", "reportArgumentType"): 1,
            added: 2,
        },
        [known],
    )

    document = json.loads(target.read_text(encoding="utf-8"))
    entries = {
        (entry["file"], entry["rule"]): entry for entry in document["exceptions"]
    }
    assert entries[("control/tests/example.py", "reportArgumentType")]["reason"] == (
        known["reason"]
    )
    assert entries[added]["count"] == 2
    assert entries[added]["reason"] == ""
    assert "control/src/example.py reportGeneralTypeIssues" in capsys.readouterr().err
