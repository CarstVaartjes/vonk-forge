"""Project runners must never sync packages into their base interpreter."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / "scripts/ensure-project-venv"


def run_helper(environment: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(HELPER), str(environment)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


@pytest.mark.parametrize("fault", ["missing-config", "base-prefix", "missing"])
def test_broken_environment_is_repaired_without_changing_base(
    tmp_path: Path, fault: str
):
    environment = tmp_path / "venv"
    # Use a disposable base to detect writes without risking the real shared Python.
    base = tmp_path / "base"
    subprocess.run(
        [sys.executable, "-m", "venv", "--without-pip", str(base)],
        check=True,
        timeout=30,
    )
    before = {
        path.relative_to(base): (path.lstat().st_mtime_ns, path.lstat().st_size)
        for path in base.rglob("*")
    }
    if fault != "missing":
        (environment / "bin").mkdir(parents=True)
        if fault == "missing-config":
            (environment / "bin/python").symlink_to(base / "bin/python")
        else:
            (environment / "pyvenv.cfg").touch()
            python = environment / "bin/python"
            python.write_text(
                f'#!/bin/sh\nexec "{Path(sys.executable).resolve()}" "$@"\n'
            )
            python.chmod(0o755)

    result = run_helper(environment)

    assert result.returncode == 0, result.stderr
    assert "Creating virtual environment" in result.stderr
    subprocess.run(
        [
            str(environment / "bin/python"),
            "-I",
            "-c",
            (
                "import sys; from pathlib import Path; "
                "assert sys.prefix != sys.base_prefix; "
                "assert Path(sys.prefix).resolve() == Path(sys.argv[1]).resolve()"
            ),
            str(environment),
        ],
        check=True,
        timeout=30,
    )
    assert before == {
        path.relative_to(base): (path.lstat().st_mtime_ns, path.lstat().st_size)
        for path in base.rglob("*")
    }
    marker = environment / "preserve"
    marker.touch()
    reused = run_helper(environment)
    assert reused.returncode == 0
    assert "Creating virtual environment" not in reused.stderr
    assert marker.exists()


@pytest.mark.parametrize("runner", ["ensure-project-venv", "sync-cli-dependencies"])
def test_base_interpreter_directory_is_never_recreated(
    tmp_path: Path, monkeypatch, runner: str
):
    called = tmp_path / "called"
    # Intercept both mutators so a broken guard cannot damage the real base.
    for name in ("uv", "rm"):
        tool = tmp_path / name
        tool.write_text(f'#!/bin/sh\ntouch "{called}"\nexit 1\n')
        tool.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    monkeypatch.setenv("VONK_CLI_DEPENDENCY_ENV", sys.base_prefix)
    result = subprocess.run(
        ["bash", str(ROOT / "scripts" / runner)]
        + ([sys.base_prefix] if runner == "ensure-project-venv" else []),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode != 0
    assert "Refusing to recreate shared base Python" in result.stderr
    assert not called.exists()


@pytest.mark.parametrize("link", ["environment", "bin", "pyvenv.cfg"])
def test_repair_refuses_symlinks_into_unowned_storage(tmp_path: Path, link: str):
    environment = tmp_path / "venv"
    outside = tmp_path / "outside"
    outside.mkdir()
    if link == "environment":
        environment.symlink_to(outside, target_is_directory=True)
    else:
        environment.mkdir()
        (environment / link).symlink_to(outside)
    result = run_helper(environment)
    assert result.returncode != 0
    assert "Refusing" in result.stderr
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("kind", ["absolute", "relative", "unsafe"])
def test_test_local_checks_environment_before_uv_run(tmp_path: Path, kind: str):
    root = tmp_path / "repo"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy(ROOT / "scripts/test-local", scripts)
    if HELPER.exists():
        shutil.copy(HELPER, scripts)
    for name, body in (
        ("build-control-wheel", "exit 0"),
        ("select-changed-tests", "cat >/dev/null; echo repository tests/probe.py"),
    ):
        script = scripts / name
        script.write_text(f"#!/bin/sh\n{body}\n")
        script.chmod(0o755)
    subprocess.run(
        ["git", "init", str(root)], check=True, capture_output=True, timeout=30
    )
    empty_tree = subprocess.run(
        ["git", "hash-object", "-w", "-t", "tree", "--stdin"],
        cwd=root,
        input="",
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    ).stdout.strip()
    binaries = tmp_path / "bin"
    binaries.mkdir()
    uv = binaries / "uv"
    real_uv = shutil.which("uv")
    assert real_uv is not None
    uv.write_text(
        '#!/bin/sh\nif [ "$1" = venv ]; then\n'
        f'  exec "{real_uv}" "$@"\nfi\n'
        f'touch "{tmp_path}/ran-uv"\n'
        "environment=$UV_PROJECT_ENVIRONMENT\n"
        'case "$environment" in /*) ;; *) environment="$PWD/control/$environment" ;; esac\n'
        'test -f "$environment/pyvenv.cfg" || exit 42\n'
        '"$environment/bin/python" -I -c '
        "'import sys; assert sys.prefix != sys.base_prefix'\n"
    )
    uv.chmod(0o755)
    environment = root / "control/broken" if kind == "relative" else tmp_path / "broken"
    (environment / "bin").mkdir(parents=True)
    (environment / "bin/python").symlink_to(sys.executable)
    if kind == "unsafe":
        linked = tmp_path / "linked"
        linked.symlink_to(environment, target_is_directory=True)
        environment = linked
    result = subprocess.run(
        ["bash", "scripts/test-local", "--changed", empty_tree],
        cwd=root,
        env={
            **os.environ,
            "PATH": f"{binaries}:{os.environ['PATH']}",
            "UV_CONTROL_PROJECT_ENVIRONMENT": "broken"
            if kind == "relative"
            else str(environment),
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if kind == "unsafe":
        assert result.returncode != 0
        assert "Refusing" in result.stderr
        assert not (tmp_path / "ran-uv").exists()
    else:
        assert result.returncode == 0, result.stdout + result.stderr
        assert (tmp_path / "ran-uv").exists()


def test_shared_check_refuses_ambiguous_relative_paths(tmp_path: Path, monkeypatch):
    called = tmp_path / "called"
    uv = tmp_path / "uv"
    uv.write_text(f'#!/bin/sh\ntouch "{called}"\nexit 1\n')
    uv.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    result = run_helper(Path("relative"))
    assert result.returncode != 0
    assert "absolute path" in result.stderr
    assert not called.exists()


def test_cli_sync_resolves_relative_override_from_project_not_caller(tmp_path: Path):
    root = tmp_path / "repo"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    for name in ("sync-cli-dependencies", "ensure-project-venv"):
        shutil.copy(ROOT / "scripts" / name, scripts)
    tools = root / "tools"
    tools.mkdir()
    shutil.copy(ROOT / "tools/cli_dependencies.py", tools)
    retry = scripts / "retry-dependency-fetch"
    retry.write_text('#!/bin/sh\nexec "$@"\n')
    retry.chmod(0o755)
    binaries = tmp_path / "bin"
    binaries.mkdir()
    real_uv = shutil.which("uv")
    assert real_uv is not None
    uv = binaries / "uv"
    uv.write_text(
        '#!/bin/sh\nif [ "$1" = venv ]; then\n'
        f'  exec "{real_uv}" "$@"\nfi\n'
        "environment=$UV_PROJECT_ENVIRONMENT\n"
        f'case "$environment" in /*) ;; *) environment="{root}/$environment" ;; esac\n'
        'test -f "$environment/pyvenv.cfg" || exit 42\n'
        '"$environment/bin/python" -I -c '
        "'import sys; assert sys.prefix != sys.base_prefix'\n"
    )
    uv.chmod(0o755)
    result = subprocess.run(
        ["bash", str(scripts / "sync-cli-dependencies")],
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": f"{binaries}:{os.environ['PATH']}",
            "VONK_CLI_DEPENDENCY_ENV": "relative",
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert (root / "relative/.vonk-platform").is_file()
    assert not (tmp_path / "relative").exists()


def test_every_project_environment_setter_uses_shared_check():
    # Reading an override is not setting it; cover shell and container command strings.
    unchecked = []
    for script in [
        *(ROOT / "scripts").rglob("*"),
        *(ROOT / ".github").rglob("*"),
    ]:
        if not script.is_file() or "__pycache__" in script.parts:
            continue
        source = script.read_text(errors="replace")
        if (
            re.search(r"\bUV_PROJECT_ENVIRONMENT\s*=", source)
            and "scripts/ensure-project-venv" not in source
        ):
            unchecked.append(str(script.relative_to(ROOT)))
    assert unchecked == [], f"Project environments bypass the shared check: {unchecked}"
