from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/verify-agent-systemd"
PACKAGED_UNITS = [
    "vonk-forge-agent.service",
    "vonk-forge-docker-firewall.service",
    "vonk-forge-package-helper.service",
    "vonk-forge-package-helper.socket",
    "vonk-forge-package-upgrade-recover.service",
]


@pytest.mark.skipif(
    shutil.which("systemd-analyze") is None,
    reason="systemd-analyze is required for installed-root verification",
)
def test_verifier_analyzes_the_packaged_rust_agent_units() -> None:
    result = subprocess.run(
        [SCRIPT, "--json"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["verify"] == "passed"
    assert report["units"] == PACKAGED_UNITS
    assert set(report["security_units"]) == {
        unit for unit in PACKAGED_UNITS if unit.endswith(".service")
    }
    assert all(
        not unit["ambient_capabilities"]
        for unit in report["security_units"].values()
    )
    assert report["security_units"][
        "vonk-forge-package-upgrade-recover.service"
    ]["cap_sys_ptrace"]
    assert all(
        not unit["cap_sys_ptrace"]
        for name, unit in report["security_units"].items()
        if name != "vonk-forge-package-upgrade-recover.service"
    )
