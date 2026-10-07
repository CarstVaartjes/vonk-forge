"""Hooks acquire their own prerequisites and never leave a poisoned lock."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import check_environment as environment


def clone(tmp_path: Path) -> Path:
    (tmp_path / "control").mkdir()
    (tmp_path / "agent_protocol/src").mkdir(parents=True)
    (tmp_path / "scripts").mkdir()
    for name in ("control/pyproject.toml", "agent_protocol/pyproject.toml"):
        (tmp_path / name).write_text("")
    (tmp_path / "control/uv.lock").write_text("package = []\n")
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


def test_fresh_hook_builds_then_syncs_instead_of_refusing(
    tmp_path, monkeypatch, capsys
):
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
            python = root / "control/.venv/bin/python"
            python.parent.mkdir(parents=True)
            python.touch()

    monkeypatch.setattr(environment, "run_preparation", run)
    monkeypatch.setattr(
        module.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a[0], 0)
    )
    assert module.main() == 0
    assert calls == [
        [str(root / "scripts/build-control-wheel")],
        ["uv", "sync", "--project", "control", "--frozen"],
    ]
    assert capsys.readouterr().err.count("Preparing control environment") == 1


@pytest.mark.parametrize("fault", ["absent", "lock", "wheel", "tools"])
def test_stale_control_inputs_trigger_preparation(tmp_path, monkeypatch, fault):
    root = clone(tmp_path)
    wheel = root / "inventory/wheels/protocol.whl"
    wheel.parent.mkdir(parents=True)
    wheel.write_bytes(b"wheel")
    digest = environment.hashlib.sha256(wheel.read_bytes()).hexdigest()
    (root / "control/uv.lock").write_text(
        f'[[package]]\nname = "vonk-agent-protocol"\nsource = {{path = "../inventory/wheels/protocol.whl"}}\nwheels = [{{hash = "sha256:{digest}"}}]\n'
    )
    venv = root / "control/.venv"
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
        with (root / "control/uv.lock").open("a") as stream:
            stream.write("# updated lock\n")
    elif fault == "wheel":
        wheel.write_bytes(b"corrupt")
    else:
        monkeypatch.setattr(
            environment.subprocess,
            "run",
            lambda *a, **k: subprocess.CompletedProcess(a[0], 1),
        )
    environment.ensure_control(root, venv)
    assert len(calls) == 2


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
    with pytest.raises(
        subprocess.CalledProcessError
        if failure == "error"
        else subprocess.TimeoutExpired
    ):
        environment.prepare(
            tmp_path, tmp_path / "lock", "probe", lambda: False, [command], timeout=0.15
        )
    if failure == "error":
        assert "network unavailable" in capfd.readouterr().err
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
        with pytest.raises(TimeoutError, match="preparation lock"):
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


PREPARATION_REMEDY = re.compile(
    r"(?:run|prepare|build|install|sync)\b[^\n]{0,200}(?:explicitly|then retry|before refreshing|tools: npm ci|index.*first)|(?:not built|no built catalog index)[^\n]{0,100}run",
    re.IGNORECASE,
)


@pytest.mark.parametrize(
    "message",
    [
        "Run scripts/build-control-wheel and uv sync --project control --frozen explicitly, then retry.",
        "Prepare TypeScript tools: npm ci --prefix control/web",
        "Build the canonical protocol wheel before refreshing its pin",
        "recipe library index is not built; run scripts/build-recipe-library",
    ],
)
def test_preparation_remedy_guard_rejects_original_refusals(message):
    assert PREPARATION_REMEDY.search(message)


def test_scripts_cannot_delegate_automatic_preparation_to_users():
    # Catch both original refusals and equivalent new instructions, including
    # shell/CI helpers. Human credentials, host installation and authority are
    # outside this preparation guard.
    failures = []
    for directory in ("scripts", ".githooks", ".github", "tools"):
        for path in (ROOT / directory).rglob("*"):
            if (
                not path.is_file()
                or path.suffix == ".json"
                or "__pycache__" in path.parts
            ):
                continue
            for number, line in enumerate(
                path.read_text(errors="replace").splitlines(), 1
            ):
                if PREPARATION_REMEDY.search(line):
                    failures.append(
                        f"{path.relative_to(ROOT)}:{number}: {line.strip()}"
                    )
    assert failures == []
    allowlist = json.loads((ROOT / "tools/remedy-text-allowlist.json").read_text())
    assert not any(
        entry["path"] in {"scripts/check-staged-code", "scripts/check-staged-python"}
        for group in ("debt", "exceptions")
        for entry in allowlist[group]
    )


def test_protocol_pin_refresh_builds_missing_wheel(tmp_path, monkeypatch):
    loader = importlib.machinery.SourceFileLoader(
        "refresh_probe", str(ROOT / "scripts/refresh-protocol-wheel-lock")
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    root = clone(tmp_path)
    monkeypatch.setattr(module, "ROOT", root)
    wheel = root / "inventory/wheels/protocol.whl"
    (root / "control/uv.lock").write_text(
        '[[package]]\nname = "vonk-agent-protocol"\nsource = {path = "../inventory/wheels/protocol.whl"}\nwheels = [{filename = "protocol.whl", hash = "sha256:old"}]\n'
    )
    calls = []

    def build(command, cwd, deadline):
        calls.append(command)
        wheel.parent.mkdir(parents=True)
        wheel.write_bytes(b"built")

    monkeypatch.setattr(environment, "run_preparation", build)
    module.main()
    assert calls == [
        [
            "uv",
            "build",
            "--project",
            "agent_protocol",
            "--wheel",
            "--out-dir",
            "inventory/wheels",
        ]
    ]
    assert (
        environment.hashlib.sha256(b"built").hexdigest()
        in (root / "control/uv.lock").read_text()
    )


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
    assert module.resolve_recipe_library(str(library), False) == (library, "pinned")
    assert calls == [[str(tmp_path / "scripts/build-recipe-library"), str(library)]]
