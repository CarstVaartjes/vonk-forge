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


def test_clock_helper_can_use_only_the_boot_caps_nvml_device_authority():
    """Catches a signed -lgc path that always waits because its sandbox denies NVML."""
    from configparser import ConfigParser

    root = Path(__file__).resolve().parents[1] / "packaging/systemd"
    helper_text = (root / "vonk-forge-package-helper.service").read_text()
    boot_text = (root / "vonk-forge-gpu-clock-cap.service").read_text()
    helper = ConfigParser(strict=False)
    boot = ConfigParser(strict=False)
    helper.read_string(helper_text)
    boot.read_string(boot_text)
    assert helper["Service"]["PrivateDevices"] == "no"
    assert helper["Service"]["DevicePolicy"] == "closed"
    boot_caps = set(boot["Service"]["CapabilityBoundingSet"].split())
    assert boot_caps <= set(helper["Service"]["CapabilityBoundingSet"].split())
    helper_devices = {
        line for line in helper_text.splitlines() if line.startswith("DeviceAllow=")
    }
    boot_devices = {
        line for line in boot_text.splitlines() if line.startswith("DeviceAllow=")
    }
    assert helper_devices == boot_devices


def test_boot_default_cannot_drift_from_the_profile_clock_policy():
    """A changed profile default must not reboot into a different safety range."""
    import shlex
    from configparser import ConfigParser

    from vonk_agent_protocol.gpu_clock import GPU_CLOCK_POLICY

    unit = ConfigParser(strict=False)
    unit.read(
        Path(__file__).resolve().parents[1]
        / "packaging/systemd/vonk-forge-gpu-clock-cap.service"
    )
    assert shlex.split(unit["Service"]["ExecStart"]) == [
        "/usr/bin/nvidia-smi",
        "-lgc",
        f"{GPU_CLOCK_POLICY.default_min_mhz},{GPU_CLOCK_POLICY.default_max_mhz}",
    ]
