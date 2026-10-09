from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.linux_only, pytest.mark.needs_systemd]

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/verify-agent-systemd"
PACKAGED_UNITS = [
    "vonk-forge-agent.service",
    "vonk-forge-monitor.service",
    "vonk-forge-docker-firewall.service",
    "vonk-forge-package-helper.service",
    "vonk-forge-package-helper.socket",
    "vonk-forge-package-upgrade-recover.service",
    "vonk-forge-package-upgrade-alert@.service",
]


def test_monitor_unit_allows_clean_start_before_agent_state_exists() -> None:
    unit = (ROOT / "packaging/systemd/vonk-forge-monitor.service").read_text()
    inaccessible = next(
        line.removeprefix("InaccessiblePaths=")
        for line in unit.splitlines()
        if line.startswith("InaccessiblePaths=")
    ).split()

    assert "-/var/lib/vonk-forge-agent/state.sqlite" in inaccessible
    assert "-/var/lib/vonk-forge/incoming" in inaccessible
    assert "/var/lib/vonk-forge-agent/state.sqlite" not in inaccessible
    assert "/var/lib/vonk-forge/incoming" not in inaccessible


def test_agent_unit_has_notify_watchdog_and_unsafe_recovery_alert_contract() -> None:
    agent = (ROOT / "packaging/systemd/vonk-forge-agent.service").read_text()
    recovery = (
        ROOT / "packaging/systemd/vonk-forge-package-upgrade-recover.service"
    ).read_text()
    alert = (
        ROOT / "packaging/systemd/vonk-forge-package-upgrade-alert@.service"
    ).read_text()

    assert "Type=notify" in agent
    assert "WatchdogSec=15min" in agent
    assert "OnFailure=vonk-forge-package-upgrade-alert@%n.service" in recovery
    assert "RestartPreventExitStatus=78" in recovery
    assert "package-upgrade.status" in alert


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
    assert report["agent_boot_recovery"] == {
        "restart": "on-failure",
        "restart_delay": "30s",
        "start_limit_interval": "0",
        "private_devices": "no",
        "device_policy": "closed",
        "type": "notify",
        "watchdog": "15min",
        "package_recovery_alert": "vonk-forge-package-upgrade-alert@%n.service",
    }
    # Template units are assessed through the instance systemd starts.
    assert set(report["security_units"]) == {
        unit.replace("@.service", "@vonk-forge-package-upgrade-recover.service")
        for unit in PACKAGED_UNITS
        if unit.endswith(".service")
    }
    assert all(
        not unit["ambient_capabilities"] for unit in report["security_units"].values()
    )
    assert report["security_units"]["vonk-forge-package-upgrade-recover.service"][
        "cap_sys_ptrace"
    ]
    assert all(
        not unit["cap_sys_ptrace"]
        for name, unit in report["security_units"].items()
        if name != "vonk-forge-package-upgrade-recover.service"
    )


def test_agent_orders_driver_without_udevadm_and_allows_namespace_recovery() -> None:
    from configparser import ConfigParser

    unit = ConfigParser(interpolation=None, strict=False)
    agent = (ROOT / "packaging/systemd/vonk-forge-agent.service").read_text()
    unit.read_string(agent)
    after = unit["Unit"]["After"].split()
    wants = unit["Unit"]["Wants"].split()
    assert "nvidia-persistenced.service" in set(wants) & set(after)
    # Startup must work on hosts without udevadm; missing GPU devices are
    # observed through the live device namespace, without a service restart.
    assert "udevadm" not in agent
    assert "ExecStartPre" not in unit["Service"]
    assert unit["Unit"]["StartLimitIntervalSec"] == "0"
    assert unit["Service"]["PrivateDevices"] == "no"
    assert unit["Service"]["DevicePolicy"] == "closed"
    assert unit["Service"]["Restart"] == "on-failure"
    assert unit["Service"]["RestartSec"] == "30s"
    assert unit["Service"]["RestartPreventExitStatus"] == "78"
