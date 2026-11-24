"""The Python type check and the commit hook that runs it on staged files."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/check-python-types"


def _module(script: Path = SCRIPT) -> ModuleType:
    loader = importlib.machinery.SourceFileLoader("check_python_types", str(script))
    specification = importlib.util.spec_from_loader(loader.name, loader)
    assert specification is not None
    module = importlib.util.module_from_spec(specification)
    loader.exec_module(module)
    return module


def test_typecheck_timeout_ends_with_a_named_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()

    def expired(command: list[str], **kwargs: object) -> None:
        assert kwargs["timeout"] == module.TYPECHECK_TIMEOUT_SECONDS
        raise subprocess.TimeoutExpired(command, module.TYPECHECK_TIMEOUT_SECONDS)

    monkeypatch.setattr(module.subprocess, "run", expired)
    with pytest.raises(SystemExit):
        module.main(["control/src/vonk_control/__init__.py"])


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("value = missing_name\n", "F821"),
        ("value=1\n", "would be reformatted"),
        ('value: int = "wrong"\n', "reportAssignmentType"),
        ("value: int = 1\n", "0 errors"),
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
        "\nimport os\n\ndef pyright_environment(): return dict(os.environ)\n"
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
    monkeypatch.setattr(module, "check_added_lines", lambda changed: None)
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
    assert prepared == [(tmp_path, tmp_path / ".venv")]


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
    ("file", "source"),
    [
        ("packaging/systemd/new.service", "Restart=no\n"),
    ],
)
def test_staged_added_line_violation_fails_the_hook(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    file: str,
    source: str,
) -> None:
    module = _module(ROOT / "scripts/check-staged-code")
    # The guards read files relative to their own checkout: copy them beside the diff.
    shutil.copytree(
        ROOT / "control/tests",
        tmp_path / "control/tests",
        ignore=shutil.ignore_patterns("test_*", "__pycache__"),
    )
    target = tmp_path / file
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source)
    patch = (
        f"diff --git a/{file} b/{file}\n--- /dev/null\n+++ b/{file}\n"
        f"@@ -0,0 +1 @@\n+{source}"
    ).encode()
    unstaged: list[str] = []

    def git(command: list[str], **kwargs: object) -> bytes:
        if "--cached" in command:
            return patch
        return "\0".join(unstaged).encode()

    monkeypatch.setattr(module.subprocess, "check_output", git)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module, "ensure_control", lambda root, env: None)
    monkeypatch.setenv(
        "UV_PROJECT_ENVIRONMENT", str(Path(sys.executable).parent.parent)
    )
    with pytest.raises(subprocess.CalledProcessError) as raised:
        module.check_added_lines([file])
    assert raised.value.returncode == 1

    # Unstaged edits on the file: skipped with its name, CI still gates.
    unstaged.append(file)
    module.check_added_lines([file])
    assert file in capsys.readouterr().err


def test_staged_extensionless_files_are_python_only_when_ruff_selects_them(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A Python script without a suffix is linted; a shell script that
    pyproject.toml's extend-exclude names is not (Ruff's selection decides)."""
    module = _hook_module()
    for name in ("scripts/tool", "scripts/run-shell", "scripts/helper.py"):
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).touch()
    monkeypatch.setattr(module, "ROOT", tmp_path)
    selection = f"{tmp_path / 'scripts/tool'}\n{tmp_path / 'scripts/helper.py'}\n"
    commands: list[list[str]] = []

    def run(command: list[str], **kwargs: object) -> SimpleNamespace:
        commands.append(command)
        return SimpleNamespace(returncode=0, stdout=selection)

    monkeypatch.setattr(module.subprocess, "run", run)
    staged = ["scripts/tool", "scripts/run-shell", "scripts/helper.py"]
    assert module.python_files(staged, tmp_path / "python") == [
        "scripts/tool",
        "scripts/helper.py",
    ]
    assert commands[0][-2:] == ["--show-files", "."]
