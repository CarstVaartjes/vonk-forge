"""Hooks acquire their own prerequisites and never leave a poisoned lock."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import check_environment as environment
import pytest

ROOT = Path(__file__).resolve().parents[2]


def clone(tmp_path: Path) -> Path:
    (tmp_path / "control").mkdir()
    (tmp_path / "agent_protocol/src").mkdir(parents=True)
    (tmp_path / "nas_setup").mkdir()
    (tmp_path / "scripts").mkdir()
    for name in (
        "pyproject.toml",
        "control/pyproject.toml",
        "agent_protocol/pyproject.toml",
        "nas_setup/pyproject.toml",
        "nas_setup/hatch_build.py",
    ):
        (tmp_path / name).write_text("")
    (tmp_path / "agent_protocol/pyproject.toml").write_bytes(
        (ROOT / "agent_protocol/pyproject.toml").read_bytes()
    )
    (tmp_path / "uv.lock").write_text("package = []\n")
    (tmp_path / "example.py").touch()
    return tmp_path


def hook():
    loader = importlib.machinery.SourceFileLoader(
        "hook_probe", str(ROOT / "scripts/check-staged-python")
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_fresh_hook_syncs_instead_of_refusing(tmp_path, monkeypatch, capsys):
    root = clone(tmp_path)
    module = hook()
    monkeypatch.setattr(module, "ROOT", root)
    monkeypatch.delenv("UV_PROJECT_ENVIRONMENT", raising=False)
    monkeypatch.setattr(
        module.subprocess, "check_output", lambda *a, **k: b"example.py\0"
    )
    calls = []

    def run(command, cwd, deadline):
        calls.append(command)
        assert cwd == root
        assert deadline > environment.time.monotonic()
        if command[0] == "uv":
            python = root / ".venv/bin/python"
            python.parent.mkdir(parents=True)
            python.touch()

    monkeypatch.setattr(environment, "run_preparation", run)
    monkeypatch.setattr(
        module.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a[0], 0)
    )
    assert module.main() == 0
    assert calls == [["uv", "sync", "--project", "control", "--frozen"]]
    assert capsys.readouterr().err.count("Preparing control environment") == 1


@pytest.mark.parametrize(
    "fault", ["absent", "lock", "tools", "nas-project", "nas-build-hook"]
)
def test_stale_control_inputs_trigger_preparation(tmp_path, monkeypatch, fault):
    root = clone(tmp_path)
    venv = root / ".venv"
    calls = []

    def run(command, cwd, deadline):
        calls.append(command)
        (venv / "bin").mkdir(parents=True, exist_ok=True)
        (venv / "bin/python").touch()

    monkeypatch.setattr(environment, "run_preparation", run)
    monkeypatch.setattr(
        environment.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 0),
    )
    environment.ensure_control(root, venv)
    calls.clear()
    environment.ensure_control(root, venv)
    assert calls == []
    if fault == "absent":
        (venv / "bin/python").unlink()
    elif fault == "lock":
        with (root / "uv.lock").open("a") as stream:
            stream.write("# updated lock\n")
    elif fault in ("nas-project", "nas-build-hook"):
        name = "pyproject.toml" if fault == "nas-project" else "hatch_build.py"
        (root / "nas_setup" / name).write_text("# changed build input\n")
    else:
        monkeypatch.setattr(
            environment.subprocess,
            "run",
            lambda *a, **k: subprocess.CompletedProcess(a[0], 1),
        )
    environment.ensure_control(root, venv)
    assert len(calls) == 1


def test_concurrent_preparation_builds_once(tmp_path):
    marker = tmp_path / "ready"
    calls = tmp_path / "calls"
    command = [
        sys.executable,
        "-c",
        f"from pathlib import Path; import time; time.sleep(.1); Path({str(calls)!r}).open('a').write('build\\n'); Path({str(marker)!r}).touch()",
    ]

    def prepare():
        environment.prepare(
            tmp_path, tmp_path / "lock", "probe", marker.exists, [command], timeout=2
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(prepare) for _ in range(2)]
        for future in futures:
            future.result()
    assert calls.read_text() == "build\n"


@pytest.mark.parametrize("failure", ["error", "timeout"])
def test_failed_preparation_releases_lock_for_next_request(tmp_path, failure, capfd):
    command = [
        sys.executable,
        "-c",
        "import sys; print('network unavailable', file=sys.stderr); sys.exit(7)"
        if failure == "error"
        else "import time; time.sleep(10)",
    ]
    with pytest.raises((subprocess.CalledProcessError, subprocess.TimeoutExpired)):
        environment.prepare(
            tmp_path, tmp_path / "lock", "probe", lambda: False, [command], timeout=0.15
        )
    # A fresh request succeeds on the same lock, without manual cleanup.
    marker = tmp_path / "ready"
    environment.prepare(
        tmp_path,
        tmp_path / "lock",
        "probe",
        marker.exists,
        [
            [
                sys.executable,
                "-c",
                f"from pathlib import Path; Path({str(marker)!r}).touch()",
            ]
        ],
        timeout=2,
    )
    assert marker.exists()


def test_lock_wait_is_bounded_and_fresh_request_succeeds(tmp_path):
    lock = tmp_path / "lock"
    with lock.open("a") as owner:
        environment.fcntl.flock(owner, environment.fcntl.LOCK_EX)
        with pytest.raises(TimeoutError):
            environment.prepare(tmp_path, lock, "probe", lambda: True, [], timeout=0.05)
    environment.prepare(tmp_path, lock, "probe", lambda: True, [], timeout=0.1)


@pytest.mark.parametrize("kind", ["web", "catalog"])
def test_other_checks_prepare_missing_inputs(tmp_path, monkeypatch, kind):
    calls = []

    def run(command, root, deadline):
        calls.append(command)
        (tmp_path / "control/web/node_modules").mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(environment, "run_preparation", run)
    if kind == "web":
        directory = tmp_path / "control/web"
        directory.mkdir(parents=True)
        for name in ("package.json", "package-lock.json"):
            (directory / name).write_text("{}")
        environment.ensure_web(tmp_path)
        assert calls == [["npm", "ci", "--prefix", "control/web"]]
    else:
        environment.ensure_catalog(tmp_path, tmp_path)
        assert calls == [
            [str(tmp_path / "scripts/build-recipe-library"), str(tmp_path)]
        ]


def test_wire_lane_builds_catalog_before_checking_revision(tmp_path, monkeypatch):
    loader = importlib.machinery.SourceFileLoader(
        "lane_probe", str(ROOT / "scripts/dev_agent_wire_linux.py")
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    monkeypatch.setattr(module, "REPO_ROOT", tmp_path)
    library = tmp_path / "library"
    library.mkdir()
    pin = tmp_path / module.RECIPE_REVISION_FILE
    pin.parent.mkdir(parents=True)
    pin.write_text("pinned")
    monkeypatch.setattr(
        module,
        "capture",
        lambda command: (0, "pinned" if command[-1] == "HEAD" else ""),
    )
    calls = []

    def build(command, cwd, deadline):
        calls.append(command)
        (library / "catalog-index.json").write_text("{}")

    monkeypatch.setattr(environment, "run_preparation", build)
    assert module.resolve_recipe_library(str(library), allow_any_revision=False) == (
        library,
        "pinned",
    )
    assert calls == [[str(tmp_path / "scripts/build-recipe-library"), str(library)]]


def test_protocol_environment_adds_only_the_protocol_member(tmp_path, monkeypatch):
    """The wire exporter needs pydantic and jsonschema, never the Controller."""
    root = clone(tmp_path)
    venv = root / ".venv"
    calls = []

    def run(command, cwd, deadline):
        calls.append(command)
        (venv / "bin").mkdir(parents=True, exist_ok=True)
        (venv / "bin/python").touch()

    monkeypatch.setattr(environment, "run_preparation", run)
    monkeypatch.setattr(
        environment.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 0),
    )
    environment.ensure_protocol(root, venv)
    # Inexact: a bare `uv sync --package` would uninstall the other members.
    assert calls == [
        [str(root / "scripts/ensure-project-venv"), str(venv.resolve())],
        [
            "env",
            f"UV_PROJECT_ENVIRONMENT={venv}",
            "uv",
            "sync",
            "--inexact",
            "--package",
            "vonk-agent-protocol",
            "--frozen",
            "--no-dev",
        ],
    ]
    environment.ensure_protocol(root, venv)
    assert len(calls) == 2  # the stamp matches the lock: nothing to do
    (root / "uv.lock").write_text("version = 2\n")
    environment.ensure_protocol(root, venv)
    assert len(calls) == 4


def test_staged_files_outside_the_pyright_scope_are_not_type_checked() -> None:
    module = hook()
    assert module.type_checked(
        [
            "deploy/compose/image-janitor/janitor.py",
            "control/tests/test_api.py",
            "src/cluster_profiles/generated_control/client.py",
            "src/cluster_profiles/cli.py",
            "scripts/check-python-types",
        ]
    ) == [
        "control/tests/test_api.py",
        "src/cluster_profiles/cli.py",
        "scripts/check-python-types",
    ]
