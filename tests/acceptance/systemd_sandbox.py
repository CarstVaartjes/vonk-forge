"""Preserve and verify shipped service isolation in disposable local acceptance."""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
from pathlib import Path

UNITS = (
    "vonk-forge-agent.service",
    "vonk-forge-monitor.service",
    "vonk-forge-package-helper.service",
    "vonk-forge-docker-firewall.service",
)
LOCAL_OVERRIDE = "zzz-lxc-service.conf"
SCALARS = frozenset(
    {
        "User",
        "Group",
        "NoNewPrivileges",
        "PrivateTmp",
        "PrivateDevices",
        "ProtectSystem",
        "ProtectHome",
        "ProtectHostname",
        "ProtectClock",
        "ProtectKernelTunables",
        "ProtectKernelModules",
        "ProtectKernelLogs",
        "ProtectControlGroups",
        "ProtectProc",
        "ProcSubset",
        "LockPersonality",
        "MemoryDenyWriteExecute",
        "RestrictRealtime",
        "RestrictSUIDSGID",
        "DevicePolicy",
        "SystemCallArchitectures",
    }
)
LISTS = frozenset(
    {
        "ReadWritePaths",
        "ReadOnlyPaths",
        "BindReadOnlyPaths",
        "BindPaths",
        "InaccessiblePaths",
        "SupplementaryGroups",
        "RestrictAddressFamilies",
    }
)


class SandboxError(RuntimeError):
    """A local service override masks the installed package's isolation."""


def _policy(document: str) -> dict[str, str]:
    section = ""
    result: dict[str, str] = {}
    for line in document.splitlines():
        line = line.strip()
        if line.startswith("["):
            section = line
        if section != "[Service]":
            continue
        key, separator, value = line.partition("=")
        if separator and key in SCALARS | LISTS:
            if key in LISTS and value:
                result[key] = (result.get(key, "") + " " + value).strip()
            else:
                result[key] = value
    return result


def _normalized(key: str, value: str) -> str | frozenset[str]:
    if key in LISTS:
        values = [item.lstrip("-") for item in shlex.split(value)]
        if key in {"BindPaths", "BindReadOnlyPaths"}:
            normalized = []
            for item in values:
                fields = item.split(":")
                source = fields[0]
                target = fields[1] if len(fields) > 1 else source
                mode = fields[2] if len(fields) > 2 else "rbind"
                normalized.append(f"{source}:{target}:{mode}")
            values = normalized
        return frozenset(values)
    return {"true": "yes", "false": "no"}.get(value.lower(), value)


def verify_effective_sandbox() -> None:
    for unit in UNITS:
        fragment = subprocess.check_output(
            ["systemctl", "show", unit, "--property=FragmentPath", "--value"],
            text=True,
            timeout=15,
        ).strip()
        if not fragment:
            raise SandboxError(f"installed service fragment is unavailable: {unit}")
        expected = _policy(Path(fragment).read_text())
        if not expected or expected.get("ProtectSystem") != "strict":
            raise SandboxError(
                f"installed service has no strict filesystem policy: {unit}"
            )
        output = subprocess.check_output(
            [
                "systemctl",
                "show",
                unit,
                "--no-pager",
                *(f"--property={key}" for key in sorted(expected)),
            ],
            text=True,
            timeout=15,
        )
        effective = dict(
            line.split("=", 1) for line in output.splitlines() if "=" in line
        )
        mismatches = [
            key
            for key, value in expected.items()
            if key not in effective
            or _normalized(key, value) != _normalized(key, effective[key])
        ]
        if mismatches:
            raise SandboxError(
                f"effective {unit} sandbox differs from its installed package: "
                + ", ".join(sorted(mismatches))
                + "; local acceptance must mask OrbStack's global service override "
                "for Vonk services before installation; see docs/runbooks/local-spark-acceptance.md"
            )


def prepare_local_sandbox() -> None:
    if os.geteuid() != 0 or not Path("/run/systemd/system").is_dir():
        raise SandboxError("a disposable root systemd environment is required")
    # A same-named per-unit mask takes precedence over the generic service.d
    # drop-in. Leave OrbStack's generator and every unrelated service untouched.
    for unit in UNITS:
        target = Path("/etc/systemd/system") / f"{unit}.d" / LOCAL_OVERRIDE
        if target.is_symlink() and os.readlink(target) == "/dev/null":
            continue
        if target.exists() or target.is_symlink():
            raise SandboxError(
                f"refusing to replace an existing local override: {target}"
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.symlink_to("/dev/null")
    subprocess.run(["systemctl", "daemon-reload"], check=True, timeout=30)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare-local", "verify"))
    parser.add_argument("--disposable-systemd", action="store_true")
    arguments = parser.parse_args()
    try:
        if arguments.action == "prepare-local":
            if not arguments.disposable_systemd:
                parser.error("prepare-local requires --disposable-systemd")
            prepare_local_sandbox()
        else:
            verify_effective_sandbox()
    except (SandboxError, OSError, subprocess.SubprocessError) as error:
        raise SystemExit(str(error)) from error


if __name__ == "__main__":
    main()
