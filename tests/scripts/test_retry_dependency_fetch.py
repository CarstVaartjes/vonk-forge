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


def _fetcher(tmp_path, monkeypatch, messages):
    executable = tmp_path / "uv"
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


@pytest.mark.parametrize("error", [
    "read: connection reset by peer",
    "Git operation failed: failed to fetch commit from https://github.com/example/repo",
    "unexpected status from registry: 503 Service Unavailable",
])
def test_transient_fetch_retries_without_publishing_partial_stdout(tmp_path, monkeypatch, capsys, error):
    module = _module()
    state = _fetcher(tmp_path, monkeypatch, [error, error, ""])
    delays = []
    monkeypatch.setattr(module.time, "sleep", delays.append)
    assert module.main(["uv", "sync", "--frozen"]) == 0
    assert json.loads(state.read_text()) == []
    assert delays == [2, 4]
    captured = capsys.readouterr()
    assert captured.out == "verified output\n"
    assert error in captured.err


@pytest.mark.parametrize("error", [
    "Git operation failed: failed to fetch: Authentication failed",
    "Git operation failed: failed to fetch: not our ref 1234",
    "No solution found when resolving dependencies",
    "manifest unknown",
    "x509: certificate signed by unknown authority",
    "digest mismatch",
])
def test_permanent_fetch_failure_is_not_retried(tmp_path, monkeypatch, error):
    module = _module()
    state = _fetcher(tmp_path, monkeypatch, [error, ""])
    assert module.main(["uv", "sync", "--frozen"]) == 17
    assert json.loads(state.read_text()) == [""]


def test_transient_failure_stops_at_attempt_limit(tmp_path, monkeypatch):
    module = _module()
    state = _fetcher(tmp_path, monkeypatch, ["connection reset by peer"] * 4)
    monkeypatch.setattr(module.time, "sleep", lambda _: None)
    assert module.main(["uv", "sync"]) == 17
    assert len(json.loads(state.read_text())) == 1


def test_cannot_wrap_test_execution(tmp_path, monkeypatch):
    module = _module()
    state = _fetcher(tmp_path, monkeypatch, [""])
    assert module.main(["uv", "run", "pytest"]) == 64
    assert json.loads(state.read_text()) == [""]
