"""Exercise acquisition retries through real child processes, never test reruns."""

import importlib.util
import json
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


@pytest.mark.parametrize(("command", "error"), [
    (["uv", "sync", "--frozen"], "read: connection reset by peer"),
    (["uv", "sync", "--frozen"], "Git operation failed: failed to fetch commit from https://github.com/example/repo"),
    (["skopeo", "inspect", "docker://example"], "unexpected status from registry: 503 Service Unavailable"),
    (["docker", "pull", "example@sha256:" + "a" * 64], "unexpected status from HEAD request: 500 Internal Server Error"),
    (["docker", "pull", "example@sha256:" + "a" * 64], "failed to copy: unexpected status code: 504 Gateway Time-out"),
])
def test_transient_fetch_retries_without_publishing_partial_stdout(tmp_path, monkeypatch, capsys, error, command):
    module = _module()
    state = _fetcher(tmp_path, monkeypatch, [error, error, ""], command[0])
    delays = []
    monkeypatch.setattr(module.time, "sleep", delays.append)
    assert module.main(command) == 0
    assert json.loads(state.read_text()) == []
    assert delays == [2, 4]
    captured = capsys.readouterr()
    assert captured.out == "verified output\n"
    assert error in captured.err


@pytest.mark.parametrize(("command", "error"), [
    (["uv", "sync", "--frozen"], "Git operation failed: failed to fetch: Authentication failed"),
    (["uv", "sync", "--frozen"], "Git operation failed: failed to fetch: not our ref 1234"),
    (["uv", "sync", "--frozen"], "No solution found when resolving dependencies"),
    (["docker", "pull", "example@sha256:" + "a" * 64], "manifest unknown"),
    (["docker", "pull", "example@sha256:" + "a" * 64], "x509: certificate signed by unknown authority"),
    (["docker", "pull", "example@sha256:" + "a" * 64], "digest mismatch"),
    (["docker", "pull", "example@sha256:" + "a" * 64], "503 Service Unavailable: unauthorized"),
])
def test_permanent_fetch_failure_is_not_retried(tmp_path, monkeypatch, error, command):
    module = _module()
    state = _fetcher(tmp_path, monkeypatch, [error, ""], command[0])
    assert module.main(command) == 17
    assert json.loads(state.read_text()) == [""]


def test_transient_failure_stops_at_attempt_limit(tmp_path, monkeypatch):
    module = _module()
    state = _fetcher(tmp_path, monkeypatch, ["connection reset by peer"] * 4)
    monkeypatch.setattr(module.time, "sleep", lambda _: None)
    assert module.main(["uv", "sync"]) == 17
    assert len(json.loads(state.read_text())) == 1


@pytest.mark.parametrize("command", [["uv", "run", "pytest"], ["docker", "build", "."], ["docker", "run", "example"]])
def test_cannot_wrap_build_or_test_execution(tmp_path, monkeypatch, command):
    module = _module()
    state = _fetcher(tmp_path, monkeypatch, [""], command[0])
    assert module.main(command) == 64
    assert json.loads(state.read_text()) == [""]
