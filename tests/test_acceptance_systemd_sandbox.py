from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.acceptance import systemd_sandbox as sandbox


@pytest.mark.parametrize(
    "override", ["ProtectSystem=no", "ReadWritePaths=", "PrivateDevices=no"]
)
def test_effective_sandbox_rejects_masked_shipped_isolation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, override: str
) -> None:
    fragment = tmp_path / "helper.service"
    fragment.write_text(
        "[Service]\nProtectSystem=strict\nPrivateDevices=yes\n"
        "ReadWritePaths=-/var/lib/vonk-forge-agent/installations\n"
    )
    key, value = override.split("=", 1)
    values = sandbox._policy(fragment.read_text()) | {key: value}

    def show(command, **_kwargs):
        if "--property=FragmentPath" in command:
            return str(fragment) + "\n"
        return "\n".join(f"{name}={setting}" for name, setting in values.items())

    monkeypatch.setattr(subprocess, "check_output", show)
    with pytest.raises(sandbox.SandboxError, match=key):
        sandbox.verify_effective_sandbox()


def test_effective_sandbox_accepts_same_policy_with_systemd_path_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fragment = tmp_path / "helper.service"
    fragment.write_text(
        "[Service]\nProtectSystem=strict\nPrivateDevices=yes\n"
        "ReadWritePaths=/var/lib/vonk-forge-agent/runs\n"
        "ReadWritePaths=-/var/lib/vonk-forge-agent/installations\n"
        "BindPaths=-/dev/fuse\n"
    )
    observed = []

    def show(command, **_kwargs):
        observed.append(command[2])
        if "--property=FragmentPath" in command:
            return str(fragment) + "\n"
        return (
            "ProtectSystem=strict\nPrivateDevices=yes\n"
            "BindPaths=-/dev/fuse:/dev/fuse:rbind\n"
            "ReadWritePaths=-/var/lib/vonk-forge-agent/installations "
            "/var/lib/vonk-forge-agent/runs\n"
        )

    monkeypatch.setattr(subprocess, "check_output", show)
    sandbox.verify_effective_sandbox()
    assert set(observed) == set(sandbox.UNITS)
