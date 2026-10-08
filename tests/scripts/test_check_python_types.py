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
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from control.tests.registry_storage import read_registry, write_registry
from tools.checker_contracts import CheckerException

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/check-python-types"
sys.path.insert(0, str(ROOT / "scripts"))


def _module(script: Path = SCRIPT) -> ModuleType:
    loader = importlib.machinery.SourceFileLoader("check_python_types", str(script))
    specification = importlib.util.spec_from_loader(loader.name, loader)
    assert specification is not None
    module = importlib.util.module_from_spec(specification)
    loader.exec_module(module)
    return module


def _exception(**overrides: object) -> CheckerException:
    entry = {
        "file": "control/tests/example.py",
        "rule": "reportArgumentType",
        "count": 1,
        "reason": "The case asserts the runtime refusal of an untyped value.",
    }
    entry.update(overrides)
    return CheckerException.model_validate_json(json.dumps(entry))


def test_a_reviewed_exception_with_a_reason_is_accepted() -> None:
    module = _module()
    key = ("control/tests/example.py", "reportArgumentType")

    assert module.evaluate({key: 1}, [_exception()]) == []


def test_typecheck_timeout_fails_with_a_visible_cause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()

    calls = []

    def expired(command: list[str], **kwargs: object):
        calls.append(command)
        assert kwargs["timeout"] == module.TYPECHECK_TIMEOUT_SECONDS
        if len(calls) <= 2:
            raise subprocess.TimeoutExpired(
                command, float(module.TYPECHECK_TIMEOUT_SECONDS)
            )
        return subprocess.CompletedProcess(command, 0, '{"generalDiagnostics": []}', "")

    monkeypatch.setattr(module, "_run_checker", expired)
    with pytest.raises(SystemExit):
        module._diagnostics()
    assert len(calls) == 2
    assert module._diagnostics() == []
    assert len(calls) == 3


@pytest.mark.parametrize("fault", ["timeout", "malformed", "shape", "exit"])
def test_unknown_checker_outcome_reobserves_and_a_fresh_check_runs(monkeypatch, fault):
    module = _module()
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if len(calls) == 1:
            if fault == "timeout":
                raise subprocess.TimeoutExpired(
                    command, module.TYPECHECK_TIMEOUT_SECONDS
                )
            return subprocess.CompletedProcess(
                command,
                2 if fault == "exit" else 0,
                "[]"
                if fault == "shape"
                else '{"generalDiagnostics": []}'
                if fault == "exit"
                else "unreadable",
                "",
            )
        return subprocess.CompletedProcess(command, 0, '{"generalDiagnostics": []}', "")

    monkeypatch.setattr(module, "_run_checker", run)
    assert module._diagnostics() == []
    assert len(calls) == 2
    assert module._diagnostics() == []
    assert len(calls) == 3


def test_exhausted_unknown_checker_outcome_does_not_poison_a_fresh_check(monkeypatch):
    module = _module()
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        text = "unreadable" if len(calls) <= 2 else '{"generalDiagnostics": []}'
        return subprocess.CompletedProcess(command, 0, text, "")

    monkeypatch.setattr(module, "_run_checker", run)
    with pytest.raises(SystemExit):
        module._diagnostics()
    assert len(calls) == 2
    assert module._diagnostics() == []
    assert len(calls) == 3


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


def test_stale_allowance_does_not_refuse_a_clean_or_fresh_check() -> None:
    module = _module()
    key = ("control/tests/example.py", "reportArgumentType")
    assert module.evaluate({}, [_exception()]) == []
    assert module.evaluate({key: 1}, [_exception()]) == []


def test_descriptive_notes_do_not_decide_checker_acceptance() -> None:
    module = _module()
    key = ("control/tests/example.py", "reportArgumentType")
    assert module.evaluate({key: 1}, [_exception(reason="")]) == []
    assert module.evaluate({}, [_exception(reason="")]) == []


def test_corrupt_stored_type_allowance_ends_then_replacement_is_admitted(tmp_path):
    module = _module()
    path = tmp_path / "baseline"
    invalid = {
        "schema_version": 1,
        "exceptions": [_exception().model_dump(), _exception().model_dump()],
    }
    from control.tests.registry_observation_cases import assert_replacement_recovers

    repaired = {"schema_version": 1, "exceptions": [_exception().model_dump()]}
    assert assert_replacement_recovers(module._load, path, invalid, repaired) == [
        _exception()
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

    document = read_registry(target)
    entries = {
        (entry["file"], entry["rule"]): entry for entry in document["exceptions"]
    }
    assert (
        entries[("control/tests/example.py", "reportArgumentType")]["reason"]
        == known.reason
    )
    assert entries[added]["count"] == 2
    assert entries[added]["reason"] == ""
    assert "control/src/example.py reportGeneralTypeIssues" in capsys.readouterr().err


def test_failed_gate_preserves_the_actual_diagnostic_location_and_cause(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _module()
    target = tmp_path / "baseline.json"
    write_registry(target, {"schema_version": 1, "exceptions": []})
    monkeypatch.setattr(module, "BASELINE", target)
    monkeypatch.setattr(module.sys, "argv", [str(SCRIPT)])
    report = {
        "generalDiagnostics": [
            {
                "file": str(ROOT / "control/src/example.py"),
                "severity": "error",
                "rule": "reportAttributeAccessIssue",
                "message": "CanonicalEvidence is not exported",
                "range": {"start": {"line": 29, "character": 0}},
            }
        ]
    }
    monkeypatch.setattr(
        module,
        "_run_checker",
        lambda *args, **kwargs: SimpleNamespace(
            stdout=json.dumps(report), stderr="", returncode=1
        ),
    )
    assert module.main() == 1
    detail = capsys.readouterr().err
    assert "control/src/example.py:30:1: reportAttributeAccessIssue" in detail
    assert "CanonicalEvidence is not exported" in detail


def test_partial_check_does_not_require_exceptions_in_unchecked_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = _module()
    target = tmp_path / "baseline.json"
    write_registry(
        target, {"schema_version": 1, "exceptions": [_exception().model_dump()]}
    )
    monkeypatch.setattr(module, "BASELINE", target)
    selected = "tests/scripts/test_check_python_types.py"
    monkeypatch.setattr(module.sys, "argv", [str(SCRIPT), selected])
    observed: list[list[str]] = []

    def diagnostics(files: list[str]):
        observed.append(files)
        return []

    monkeypatch.setattr(module, "_diagnostics", diagnostics)
    assert module.main() == 0
    assert observed == [[selected]]


def test_partial_check_still_rejects_new_errors_and_stale_selected_exceptions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _module()
    target = tmp_path / "baseline.json"
    write_registry(
        target, {"schema_version": 1, "exceptions": [_exception().model_dump()]}
    )
    monkeypatch.setattr(module, "BASELINE", target)
    monkeypatch.setattr(module.sys, "argv", [str(SCRIPT), "control/tests/example.py"])
    monkeypatch.setattr(
        module,
        "_diagnostics",
        lambda files: [
            module.CheckerFinding(
                file="control/tests/example.py",
                rule="reportReturnType",
                message="str is not assignable to int",
                line=1,
                column=1,
            )
        ],
    )
    assert module.main() == 1
    detail = capsys.readouterr().err
    assert "unlisted type error" in detail
    assert "str is not assignable to int" in detail


def test_partial_check_cannot_rewrite_the_full_baseline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    monkeypatch.setattr(
        module.sys, "argv", [str(SCRIPT), "--update", "tests/example.py"]
    )
    with pytest.raises(SystemExit) as failure:
        module.main()
    assert failure.value.code == 2


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("value = missing_name\n", "F821"),
        ("value=1\n", "would be reformatted"),
        ('value: int = "wrong"\n', "reportAssignmentType"),
        ("value: int = 1\n", "no unlisted errors"),
    ],
)
def test_real_hook_checks_whitespace_paths_with_prepared_environment(
    tmp_path: Path, source: str, expected: str
) -> None:
    """Catch skipped typing and broken filename splitting after preparation."""
    (tmp_path / "control").mkdir()
    (tmp_path / "scripts").mkdir()
    (tmp_path / "tools").mkdir()
    (tmp_path / "control/pyproject.toml").write_text(
        '[project]\nname = "hook-probe"\nversion = "0.0.0"\nrequires-python = ">=3.14"\n'
    )
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pyright]\ntypeCheckingMode = "basic"\n'
    )
    write_registry(
        tmp_path / "tools/pyright-baseline", {"schema_version": 1, "exceptions": []}
    )
    shutil.copy2(
        ROOT / "tools/registry_contracts.py", tmp_path / "tools/registry_contracts.py"
    )
    shutil.copy2(
        ROOT / "tools/checker_contracts.py", tmp_path / "tools/checker_contracts.py"
    )
    helper_package = tmp_path / "control/tests"
    helper_package.mkdir(parents=True)
    shutil.copy2(
        ROOT / "control/tests/registry_storage.py",
        helper_package / "registry_storage.py",
    )
    shutil.copy2(SCRIPT, tmp_path / "scripts/check-python-types")
    shutil.copy2(
        ROOT / "scripts/check-staged-code", tmp_path / "scripts/check-staged-code"
    )
    shutil.copy2(
        ROOT / "scripts/check-staged-python", tmp_path / "scripts/check-staged-python"
    )
    shutil.copy2(
        ROOT / "scripts/hook_words_generated.py",
        tmp_path / "scripts/hook_words_generated.py",
    )
    # This fixture tests check dispatch, not dependency acquisition.
    helper = tmp_path / "scripts/check_environment.py"
    helper.write_text(
        "def ensure_control(root, environment): pass\n\ndef ensure_web(root): pass\n"
    )
    probe = tmp_path / 'probe with space and "quote".py'
    probe.write_text(source)
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True, timeout=30)
    subprocess.run(
        ["git", "add", "--", probe.name], cwd=tmp_path, check=True, timeout=30
    )

    def authored_paths() -> set[Path]:
        return {
            path.relative_to(tmp_path)
            for path in tmp_path.rglob("*")
            if ".ruff_cache" not in path.parts
        }

    before = authored_paths()
    completed = subprocess.run(
        ["bash", str(ROOT / ".githooks/pre-commit")],
        cwd=tmp_path,
        env={
            **os.environ,
            "UV_PROJECT_ENVIRONMENT": str(Path(sys.executable).parent.parent),
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert expected in completed.stdout + completed.stderr
    assert completed.returncode == (0 if source == "value: int = 1\n" else 1)
    assert authored_paths() == before
    assert probe.read_text() == source


@pytest.mark.parametrize(
    "file", ["control/web/src/probe.ts", "rust/crates/probe/src/lib.rs"]
)
def test_non_python_check_failure_still_blocks_the_commit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, file: str
) -> None:
    """A Python-only early return must not silently accept other languages."""
    module = _module(ROOT / "scripts/check-staged-code")
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(
        module.subprocess, "check_output", lambda *a, **k: (file + "\0").encode()
    )
    shutil.copy2(ROOT / "rust-toolchain.toml", tmp_path / "rust-toolchain.toml")
    tools = tmp_path / "control/web/node_modules/.bin"
    tools.mkdir(parents=True)
    (tools / "biome").touch()
    (tools / "tsc").touch()

    def refuse(command: list[str], **kwargs: object) -> None:
        if command == ["python3", "scripts/check-staged-python"]:
            return
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(module, "ensure_web", lambda root: None)
    monkeypatch.setattr(module, "run", refuse)
    monkeypatch.setattr(
        module, "run_vm_cargo", lambda command, deadline: refuse(command)
    )
    with pytest.raises(subprocess.CalledProcessError):
        module.main()


def _hook_module() -> ModuleType:
    loader = importlib.machinery.SourceFileLoader(
        "check_staged_python", str(ROOT / "scripts/check-staged-python")
    )
    specification = importlib.util.spec_from_loader(loader.name, loader)
    assert specification is not None
    module = importlib.util.module_from_spec(specification)
    loader.exec_module(module)
    return module


def test_documentation_commit_needs_no_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = _hook_module()
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(
        module.subprocess, "check_output", lambda *args, **kwargs: b"docs/example.md\0"
    )
    assert module.main() == 0


def test_python_commit_requests_environment_preparation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = _hook_module()
    (tmp_path / "example.py").touch()
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.delenv("UV_PROJECT_ENVIRONMENT", raising=False)
    monkeypatch.setattr(
        module.subprocess, "check_output", lambda *args, **kwargs: b"example.py\0"
    )
    prepared = []
    monkeypatch.setattr(
        module, "ensure_control", lambda root, env: prepared.append((root, env))
    )
    monkeypatch.setattr(
        module.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0)
    )
    assert module.main() == 0
    assert prepared == [(tmp_path, tmp_path / "control/.venv")]


def test_hook_preserves_nul_filenames_and_only_runs_checks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    module = _hook_module()
    filename = "with spaces\nand newline.py"
    (tmp_path / filename).touch()
    environment = tmp_path / "prepared"
    (environment / "bin").mkdir(parents=True)
    (environment / "bin/python").touch()
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", str(environment))
    monkeypatch.setattr(module.sys, "prefix", str(environment))
    monkeypatch.setattr(
        module.subprocess,
        "check_output",
        lambda *args, **kwargs: (filename + "\0").encode(),
    )
    monkeypatch.setattr(module, "ensure_control", lambda root, env: None)
    commands: list[list[str]] = []

    def run(command: list[str], **kwargs: object) -> SimpleNamespace:
        commands.append(command)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(module.subprocess, "run", run)
    assert module.main() == 0
    assert commands == [
        [
            module.sys.executable,
            "-m",
            "ruff",
            "check",
            "--force-exclude",
            "--",
            filename,
        ],
        [
            module.sys.executable,
            "-m",
            "ruff",
            "format",
            "--check",
            "--force-exclude",
            "--",
            filename,
        ],
        [module.sys.executable, "scripts/check-python-types", filename],
    ]


@pytest.mark.parametrize(
    "diagnostic",
    [
        None,
        {},
        {"severity": "error"},
        {
            "severity": "error",
            "file": "/outside.py",
            "message": "failure",
            "range": {"start": {"line": 0, "character": 0}},
        },
        {
            "severity": "error",
            "file": str(ROOT / "example.py"),
            "message": "failure",
            "range": {"start": {"line": "zero", "character": 0}},
        },
    ],
)
def test_nested_unusable_reply_ends_then_a_fresh_observation_runs(
    monkeypatch, diagnostic
):
    module = _module()
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        body = (
            {"generalDiagnostics": [diagnostic]}
            if len(calls) <= 2
            else {"generalDiagnostics": []}
        )
        return subprocess.CompletedProcess(command, 0, json.dumps(body), "")

    monkeypatch.setattr(module, "_run_checker", run)
    with pytest.raises(SystemExit):
        module._diagnostics()
    assert len(calls) == 2
    assert module._diagnostics() == []
    assert len(calls) == 3


def test_malformed_nested_reply_is_reobserved_before_acceptance(monkeypatch):
    module = _module()
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        body = (
            {"generalDiagnostics": [None]}
            if len(calls) == 1
            else {"generalDiagnostics": []}
        )
        return subprocess.CompletedProcess(command, 0, json.dumps(body), "")

    monkeypatch.setattr(module, "_run_checker", run)
    assert module._diagnostics() == []
    assert len(calls) == 2
    assert module._diagnostics() == []
    assert len(calls) == 3


def test_checker_timeout_reaps_descendants_before_a_fresh_observation(monkeypatch):
    module = _module()
    real = module._run_checker
    calls = []
    process_tree = "import subprocess,sys,time; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); time.sleep(60)"

    def run(command, **kwargs):
        calls.append(command)
        source = (
            process_tree if len(calls) <= 2 else "print('{\"generalDiagnostics\": []}')"
        )
        return real(
            [sys.executable, "-c", source],
            env=os.environ.copy(),
            timeout=0.1 if len(calls) <= 2 else 5,
        )

    monkeypatch.setattr(module, "_run_checker", run)
    with pytest.raises(SystemExit):
        module._diagnostics(["example.py"])
    assert len(calls) == 2
    assert module._diagnostics(["example.py"]) == []
    assert len(calls) == 3
