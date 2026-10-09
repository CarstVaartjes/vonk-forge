"""Verify the packaged agent unit can recover after more than five boot races.

Run only in a disposable Linux systemd machine. The service executable is a
small fault-injection probe; it fails seven starts, then succeeds. The test
uses the packaged unit's start-limit and restart policy and never resets the
failed state between attempts.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
import time
import uuid
from configparser import ConfigParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UNIT = ROOT / "packaging/systemd/vonk-forge-agent.service"
TRANSIENT_FAILURES = 7


def run(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        arguments,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if check and result.returncode:
        raise AssertionError(f"{arguments[0]} failed: {result.stderr[-2000:]}")
    return result


def verify(unit_path: Path) -> dict[str, object]:
    unit = ConfigParser(interpolation=None, strict=False)
    if not unit.read(unit_path):
        raise RuntimeError("packaged agent unit is unavailable")
    start_limit_interval = unit["Unit"].get("StartLimitIntervalSec")
    restart = unit["Service"].get("Restart")
    prevent = unit["Service"].get("RestartPreventExitStatus", "")
    service_type = unit["Service"].get("Type")
    watchdog = unit["Service"].get("WatchdogSec")
    if (
        start_limit_interval != "0"
        or prevent
        or restart != "always"
        or service_type != "notify"
        or watchdog != "15min"
    ):
        raise RuntimeError("packaged unit does not allow automatic boot recovery")

    with tempfile.TemporaryDirectory(prefix="vonk-agent-boot-retry-") as temporary:
        directory = Path(temporary)
        attempts_path = directory / "attempts"
        success_path = directory / "recovered"
        probe = directory / "probe.py"
        probe.write_text(
            "import pathlib, sys\n"
            "attempts, success, transient_failures = pathlib.Path(sys.argv[1]), "
            "pathlib.Path(sys.argv[2]), int(sys.argv[3])\n"
            "count = int(attempts.read_text()) + 1 if attempts.exists() else 1\n"
            "attempts.write_text(str(count))\n"
            "if count <= transient_failures: raise SystemExit(78)\n"
            "success.touch()\n"
            "import time\n"
            "while True: time.sleep(1)\n"
        )
        identifier = uuid.uuid4().hex[:12]
        result = run(
            "/usr/bin/systemd-run",
            "--system",
            "--quiet",
            "--collect",
            "--service-type=exec",
            f"--unit=vonk-agent-boot-retry-{identifier}",
            f"--property=StartLimitIntervalSec={start_limit_interval}",
            f"--property=Restart={restart}",
            f"--property=RestartPreventExitStatus={prevent}",
            # Keep the retry loop short in this disposable behavioral probe.
            "--property=RestartSec=100ms",
            "/usr/bin/python3",
            str(probe),
            str(attempts_path),
            str(success_path),
            str(TRANSIENT_FAILURES),
            check=False,
        )
        if result.returncode != 0:
            raise AssertionError(
                "systemd did not recover the probe after transient failures: "
                + result.stderr[-2000:]
            )
        try:
            deadline = time.monotonic() + 15
            while not success_path.is_file() and time.monotonic() < deadline:
                time.sleep(0.1)
            attempts = int(attempts_path.read_text())
        finally:
            run("/usr/bin/systemctl", "stop", f"vonk-agent-boot-retry-{identifier}")
        if not success_path.is_file() or attempts != TRANSIENT_FAILURES + 1:
            raise AssertionError(
                f"expected recovery on attempt {TRANSIENT_FAILURES + 1}, got {attempts}"
            )
    return {
        "automatic_recovery": "passed",
        "failed_starts_before_success": TRANSIENT_FAILURES,
        "manual_reset_failed_used": False,
        "start_limit_interval": start_limit_interval,
        "service_type": service_type,
        "watchdog": watchdog,
        "systemd_attempts": attempts,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--disposable-systemd", action="store_true", required=True)
    parser.parse_args()
    if os.geteuid() != 0 or not Path("/run/systemd/system").is_dir():
        raise SystemExit("a disposable root systemd environment is required")
    report = verify(UNIT)
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
