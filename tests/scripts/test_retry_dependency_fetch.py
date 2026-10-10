"""Exercise acquisition retries through real child processes, never test reruns."""

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _module():
    spec = importlib.util.spec_from_file_location(
        "retry_dependency_fetch", ROOT / "scripts/retry_dependency_fetch.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fetcher(tmp_path, monkeypatch, messages, executable_name="uv"):
    executable = tmp_path / executable_name
    state = tmp_path / "attempts.json"
    state.write_text(json.dumps(messages))
    executable.write_text(
        f"#!{sys.executable}\n"
        "import json, pathlib, sys\n"
        f"state = pathlib.Path({str(state)!r})\n"
        "messages = json.loads(state.read_text())\n"
        "message = messages.pop(0)\n"
        "state.write_text(json.dumps(messages))\n"
        "if message:\n"
        "    print('partial untrusted output')\n"
        "    print(message, file=sys.stderr)\n"
        "    sys.exit(17)\n"
        "print('verified output')\n"
    )
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    return state


@pytest.mark.parametrize(
    ("command", "error"),
    [
        (["uv", "sync", "--frozen"], "read: connection reset by peer"),
        (
            ["uv", "sync", "--frozen"],
            "Git operation failed: failed to fetch commit from https://github.com/example/repo",
        ),
        (
            ["skopeo", "inspect", "docker://example"],
            "unexpected status from registry: 503 Service Unavailable",
        ),
        (
            ["docker", "pull", "example@sha256:" + "a" * 64],
            "unexpected status from HEAD request: 500 Internal Server Error",
        ),
        (
            ["docker", "pull", "example@sha256:" + "a" * 64],
            "failed to copy: unexpected status code: 504 Gateway Time-out",
        ),
        (
            ["git", "fetch", "--depth=1", "origin", "a" * 40],
            "fatal: unable to access: Could not resolve host: github.com",
        ),
        (["npm", "ci"], "npm error code ECONNRESET"),
        (
            ["cargo", "fetch", "--locked"],
            "warning: spurious network error (2 tries remaining)",
        ),
        (
            ["rustup", "toolchain", "install", "1.98.1"],
            "error: could not download file: operation timed out",
        ),
        (["playwright", "install", "chromium"], "Error: socket hang up"),
        (
            ["docker", "pull", "example@sha256:" + "a" * 64],
            (
                'Get "https://auth.docker.io/token": context deadline exceeded '
                "(Client.Timeout exceeded while awaiting headers)"
            ),
        ),
        (["uv", "sync", "--frozen"], "an error no list of known failures names"),
    ],
)
def test_transient_fetch_retries_without_publishing_partial_stdout(
    tmp_path, monkeypatch, capsys, error, command
):
    module = _module()
    state = _fetcher(tmp_path, monkeypatch, [error, error, ""], command[0])
    delays = []
    monkeypatch.setattr(module, "sleep", delays.append)
    assert module.main(command) == 0
    assert json.loads(state.read_text()) == []
    assert delays and all(0 < delay <= 4 for delay in delays)
    captured = capsys.readouterr()
    assert captured.out == "verified output\n"
    assert "partial untrusted output" not in captured.out


@pytest.mark.parametrize(
    ("command", "error"),
    [
        (
            ["uv", "sync", "--frozen"],
            "Git operation failed: failed to fetch: Authentication failed",
        ),
        (
            ["uv", "sync", "--frozen"],
            "Git operation failed: failed to fetch: not our ref 1234",
        ),
        (["uv", "sync", "--frozen"], "No solution found when resolving dependencies"),
        (["docker", "pull", "example@sha256:" + "a" * 64], "manifest unknown"),
        (
            ["docker", "pull", "example@sha256:" + "a" * 64],
            "x509: certificate signed by unknown authority",
        ),
        (["docker", "pull", "example@sha256:" + "a" * 64], "digest mismatch"),
        (
            ["docker", "pull", "example@sha256:" + "a" * 64],
            "503 Service Unavailable: unauthorized",
        ),
    ],
)
def test_permanent_fetch_failure_is_not_retried(tmp_path, monkeypatch, error, command):
    module = _module()
    state = _fetcher(tmp_path, monkeypatch, [error, ""], command[0])
    assert module.main(command) == 17
    assert json.loads(state.read_text()) == [""]
    assert module.main(command) == 0
    assert json.loads(state.read_text()) == []


def test_transient_failure_stops_at_attempt_limit(tmp_path, monkeypatch):
    module = _module()
    state = _fetcher(
        tmp_path, monkeypatch, ["connection reset by peer"] * (module.MAX_ATTEMPTS + 1)
    )
    monkeypatch.setattr(module, "sleep", lambda _: None)
    assert module.main(["uv", "sync"]) == 17
    assert len(json.loads(state.read_text())) == 1
    state.write_text(json.dumps([""]))
    assert module.main(["uv", "sync"]) == 0
    assert json.loads(state.read_text()) == []


@pytest.mark.parametrize(
    "command",
    [
        ["uv", "run", "pytest"],
        ["docker", "build", "."],
        ["docker", "run", "example"],
        ["cargo", "build"],
        ["npm", "test"],
        ["git", "push"],
    ],
)
def test_cannot_wrap_build_or_test_execution(tmp_path, monkeypatch, command):
    module = _module()
    state = _fetcher(tmp_path, monkeypatch, [""], command[0])
    assert module.main(command) == 64
    assert json.loads(state.read_text()) == [""]


def test_failure_past_the_deadline_is_not_retried(tmp_path, monkeypatch):
    module = _module()
    state = _fetcher(tmp_path, monkeypatch, ["connection reset by peer", ""])
    monkeypatch.setattr(module, "FETCH_DEADLINE_SECONDS", 0)
    monkeypatch.setattr(module, "sleep", lambda _: None)
    assert module.main(["uv", "sync"]) == 17
    assert json.loads(state.read_text()) == [""]


def test_expired_fetch_reaps_child_and_resumes_without_partial_stdout(
    tmp_path, monkeypatch, capsys
):
    module = _module()
    executable = tmp_path / "uv"
    marker = tmp_path / "first-pid"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import os, pathlib, time\n"
        f"marker = pathlib.Path({str(marker)!r})\n"
        "if not marker.exists():\n"
        "    marker.write_text(str(os.getpid()))\n"
        "    print('partial output', flush=True)\n"
        "    time.sleep(3600)\n"
        "print('verified output')\n"
    )
    executable.chmod(0o755)
    monkeypatch.setattr(module, "FETCH_TIMEOUT_SECONDS", 1)
    monkeypatch.setattr(module, "sleep", lambda _: None)
    assert module.main([str(executable), "sync"]) == 0
    captured = capsys.readouterr()
    assert captured.out == "verified output\n"
    assert "partial output" in captured.err
    assert module.main([str(executable), "sync"]) == 0
    assert capsys.readouterr().out == "verified output\n"
    with pytest.raises(ProcessLookupError):
        os.kill(int(marker.read_text()), 0)


def _locked_temporary_pip_command(tmp_path, monkeypatch):
    runner = tmp_path / "runner"
    python = runner / "dependencies/bin/python"
    python.parent.mkdir(parents=True)
    python.write_text("isolated interpreter fixture")
    (python.parent.parent / "pyvenv.cfg").write_text(
        "home = isolated-test-interpreter\n"
    )
    requirements = runner / "locked-requirements.txt"
    requirements.write_text("dependency==1 --hash=sha256:" + "a" * 64 + "\n")
    cache = runner / "cache"
    cache.mkdir()
    monkeypatch.setenv("RUNNER_TEMP", str(runner))
    monkeypatch.setenv("UV_CACHE_DIR", str(cache))
    return [
        "uv",
        "pip",
        "install",
        "--python",
        str(python),
        "--reinstall",
        "--require-hashes",
        "-r",
        str(requirements),
    ]


def test_locked_temporary_pip_acquisition_retries_real_child_without_partial_output(
    tmp_path, monkeypatch, capsys
):
    module = _module()
    command = _locked_temporary_pip_command(tmp_path, monkeypatch)
    state = _fetcher(tmp_path, monkeypatch, ["connection reset by peer", ""])
    delays = []
    monkeypatch.setattr(module, "sleep", delays.append)
    assert module.main(command) == 0
    assert json.loads(state.read_text()) == []
    assert len(delays) == 1 and 1 <= delays[0] <= 2
    assert capsys.readouterr().out == "verified output\n"


@pytest.mark.parametrize(
    "fault",
    [
        "without-hashes",
        "extra-package",
        "outside-venv",
        "outside-cache",
        "outside-requirements",
        "missing-venv",
    ],
)
def test_unbounded_pip_install_is_refused_before_real_child(
    tmp_path, monkeypatch, fault
):
    module = _module()
    command = _locked_temporary_pip_command(tmp_path, monkeypatch)
    if fault == "without-hashes":
        command.remove("--require-hashes")
    elif fault == "extra-package":
        command.append("unlocked-package")
    elif fault == "outside-venv":
        command[4] = sys.executable
    elif fault == "outside-cache":
        monkeypatch.setenv("UV_CACHE_DIR", str(tmp_path / "foreign-cache"))
    elif fault == "outside-requirements":
        command[-1] = str(tmp_path / "foreign-requirements.txt")
        Path(command[-1]).write_text("foreign requirements")
    elif fault == "missing-venv":
        (Path(command[4]).parent.parent / "pyvenv.cfg").unlink()
    state = _fetcher(tmp_path, monkeypatch, [""])
    assert module.main(command) == 64
    assert json.loads(state.read_text()) == [""]


@pytest.mark.parametrize(
    "error",
    [
        "Hash mismatch: connection reset by peer",
        "403 Forbidden: connection reset by peer",
    ],
)
def test_locked_pip_integrity_and_authorization_faults_never_retry(
    tmp_path, monkeypatch, error
):
    module = _module()
    command = _locked_temporary_pip_command(tmp_path, monkeypatch)
    state = _fetcher(tmp_path, monkeypatch, [error, ""])
    assert module.main(command) == 17
    assert json.loads(state.read_text()) == [""]
