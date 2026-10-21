"""Prevent NVML sensor queries being denied by the monitor device cgroup."""

from pathlib import Path


def test_monitor_grants_nvml_read_write_device_open():
    # An r-only grant exposes nodes but denies NVML's O_RDWR open.
    unit = (
        Path(__file__).resolve().parents[1]
        / "packaging/systemd/vonk-forge-monitor.service"
    ).read_text()
    grants = [
        line.removeprefix("DeviceAllow=").split()
        for line in unit.splitlines()
        if line.startswith("DeviceAllow=")
    ]
    assert grants
    assert all(
        "r" in mode and "w" in mode
        for path, mode in grants
        if path.startswith("/dev/nvidia")
    )
    assert "DevicePolicy=closed" in unit
    assert "CapabilityBoundingSet=\n" in unit


def test_clock_helper_keeps_nvml_device_authority():
    """Catches a policy apply sandbox that denies NVML's O_RDWR device opens."""
    from configparser import ConfigParser

    root = Path(__file__).resolve().parents[1] / "packaging/systemd"
    helper_text = (root / "vonk-forge-package-helper.service").read_text()
    helper = ConfigParser(strict=False)
    helper.read_string(helper_text)
    assert helper["Service"]["PrivateDevices"] == "no"
    assert helper["Service"]["DevicePolicy"] == "closed"
    assert "CAP_SYS_ADMIN" in helper["Service"]["CapabilityBoundingSet"].split()
    grants = [
        line
        for line in helper_text.splitlines()
        if line.startswith("DeviceAllow=/dev/nvidia")
    ]
    assert grants
    assert all(line.split()[-1] == "rw" for line in grants)
    # There must be one writer: a boot unit cannot overwrite a custom range.
    assert not any("-lgc" in unit.read_text() for unit in root.glob("*.service"))
