"""An exited Git process is usable only after its inventory body decodes."""

import importlib.machinery
import importlib.util
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _module():
    loader = importlib.machinery.SourceFileLoader(
        "principle_history", str(ROOT / "scripts/check-principle-history")
    )
    specification = importlib.util.spec_from_loader(loader.name, loader)
    assert specification is not None
    module = importlib.util.module_from_spec(specification)
    loader.exec_module(module)
    return module


@pytest.mark.parametrize("body", ["unreadable", "[]", "null"])
def test_successful_process_with_unusable_body_ends_and_fresh_observation_runs(
    monkeypatch, body
):
    module = _module()
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(
            command, 0, body if len(calls) <= 2 else "{}", ""
        )

    monkeypatch.setattr(module.subprocess, "run", run)
    with pytest.raises(RuntimeError):
        module._observe_git(
            ["git", "show"], check=True, timeout=10, decode=module._object
        )
    assert len(calls) == 2
    assert (
        module._object(
            module._observe_git(
                ["git", "show"], check=True, timeout=10, decode=module._object
            ).stdout
        )
        == {}
    )
    assert len(calls) == 3
