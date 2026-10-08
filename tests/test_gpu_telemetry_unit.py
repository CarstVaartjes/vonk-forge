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
