from __future__ import annotations

import ast
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
PACKAGE = ROOT / "control/src/vonk_control"


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    return imported


def test_production_api_and_worker_do_not_import_direct_runtime_or_subprocess() -> None:
    for name in ("api.py", "worker.py"):
        path = PACKAGE / name
        imports = _imports(path)
        source = path.read_text()
        assert "subprocess" not in imports
        assert "vonk_control.runtime" not in imports
        assert "vonk_control.legacy_runtime" not in imports
        assert "RuntimeHandlers" not in source
        assert "LegacyRuntimeHandlers" not in source

    worker = (PACKAGE / "worker.py").read_text()
    worker_imports = _imports(PACKAGE / "worker.py")
    assert {
        "git_policy",
        "code_host",
        "hermes_routes",
        "legacy_runtime",
        "runtime",
        "subprocess",
    }.isdisjoint(worker_imports)
    for dynamic_escape in (
        "importlib",
        "__import__",
        "os.system",
        "os.popen",
        "eval(",
        "exec(",
    ):
        assert dynamic_escape not in worker
    for forbidden in (
        "vonk_control.git_policy",
        "vonk_control.code_host",
        "vonk_control.hermes_routes",
        "GitPolicy",
    ):
        assert forbidden not in worker


def test_runtime_module_contains_no_process_or_transport_implementation() -> None:
    path = PACKAGE / "runtime.py"
    imports = _imports(path)
    source = path.read_text()

    assert "subprocess" not in imports
    assert "RuntimeHandlers" not in source
    assert "run_bounded" not in source
    assert "ssh" not in source.lower()


def test_retired_runtime_modules_are_absent() -> None:
    assert not (PACKAGE / "legacy_runtime.py").exists()
    assert not (PACKAGE / "legacy_route_runtime.py").exists()


def test_production_images_have_no_git_or_ssh_transport_tools() -> None:
    dockerfile = (ROOT / "control/Dockerfile").read_text().lower()
    worker = dockerfile.split(" as worker\n", 1)[1].split(" as api\n", 1)[0]
    api = dockerfile.split(" as api\n", 1)[1]

    for stage in (worker, api):
        assert "apt-get install" not in stage
        assert "openssh" not in stage
        assert " git" not in stage


def test_production_worker_has_no_cluster_egress_network() -> None:
    compose = (ROOT / "deploy/compose/compose.yaml").read_text()
    worker = compose.split("\n  control-worker:\n", 1)[1].split("\n  ", 1)[0]
    for forbidden in (
        "/repository",
        "git-signing-key",
        "token-signing-key",
        "metrics-token",
        "VONK_REPOSITORY_PATH",
        "VONK_GIT_SIGNING_KEY_FILE",
    ):
        assert forbidden not in worker
    assert "CONTROL_WORKER_IMAGE" in worker


def test_built_worker_image_contains_no_direct_transport_executable() -> None:
    if shutil.which("docker") is None:
        pytest.skip("Docker CLI is unavailable")
    if subprocess.run(
        ["docker", "info"],
        capture_output=True,
        check=False,
    ).returncode != 0:
        pytest.skip("Docker daemon is unavailable")
    image = "vonk-control-worker:test-no-routine-ssh"
    build = subprocess.run(
        [
            "docker",
            "build",
            "--file",
            "control/Dockerfile",
            "--target",
            "worker",
            "--tag",
            image,
            ".",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert build.returncode == 0, build.stderr
    inspect = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--entrypoint",
            "/bin/sh",
            image,
            "-eu",
            "-c",
            (
                "! command -v git; "
                "for executable in ssh scp vonkctl; do "
                "! command -v \"$executable\"; done; "
                "test ! -e /repository; test ! -e /vonk-cluster-profiles; "
                "test ! -e /scripts"
            ),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert inspect.returncode == 0, inspect.stderr
