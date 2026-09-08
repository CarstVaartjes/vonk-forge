#!/usr/bin/env python3
"""Exercise the packaged helper's filesystem policy on disposable systemd."""

from __future__ import annotations

import argparse
import os
import shlex
import shutil
import subprocess
import uuid
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--disposable-systemd", action="store_true", required=True)
    parser.add_argument(
        "--unit",
        type=Path,
        default=Path(__file__).resolve().parents[2]
        / "packaging/systemd/vonk-forge-package-helper.service",
    )
    arguments = parser.parse_args()
    if os.geteuid() != 0 or not Path("/run/systemd/system").is_dir():
        raise SystemExit("a disposable root systemd environment is required")

    # Read the shipped policy instead of duplicating its writable-path list.
    properties: dict[str, list[str]] = {}
    for line in arguments.unit.read_text().splitlines():
        key, separator, value = line.partition("=")
        if separator and key in {"ProtectSystem", "ReadWritePaths"}:
            properties.setdefault(key, []).append(value)
    if properties.get("ProtectSystem") != ["strict"]:
        raise SystemExit("the helper must retain ProtectSystem=strict")
    writable = " ".join(properties["ReadWritePaths"])
    agent_root = Path("/var/lib/vonk-forge-agent")
    identity = str(uuid.uuid4())
    installation = agent_root / "installations" / identity
    protected = agent_root / f"filesystem-proof-{identity}"
    created: list[Path] = []

    def ensure_directory(path: Path) -> None:
        if path.is_dir():
            return
        ensure_directory(path.parent)
        path.mkdir(mode=0o700)
        created.append(path)

    try:
        # Required package paths must exist when systemd constructs the namespace.
        for value in shlex.split(writable):
            if not value.startswith("-"):
                ensure_directory(Path(value))
        ensure_directory(installation / "models")
        ensure_directory(installation / "runtime-cache")
        ensure_directory(protected)
        model = installation / "models" / "configuration.json"
        model.write_text("{}\n")
        model.chmod(0o600)
        probe = """
import errno
import pathlib
import subprocess
import sys
installation, protected = map(pathlib.Path, sys.argv[1:])
subprocess.run(['/usr/bin/setfacl', '-m', 'u:10001:r',
                str(installation / 'models/configuration.json')], check=True)
home = installation / 'runtime-cache/home'
home.mkdir(mode=0o700)
(home / 'cache-proof').write_text('writable')
try:
    (protected / 'must-remain-read-only').write_text('unsafe')
except OSError as error:
    assert error.errno == errno.EROFS, error
else:
    raise AssertionError('unrelated agent state became writable')
"""
        subprocess.run(
            [
                "systemd-run",
                "--quiet",
                "--wait",
                "--pipe",
                "--collect",
                f"--unit=vonk-helper-filesystem-proof-{identity}",
                "--property=ProtectSystem=strict",
                f"--property=ReadWritePaths={writable}",
                "/usr/bin/python3",
                "-c",
                probe,
                str(installation),
                str(protected),
            ],
            check=True,
            timeout=30,
        )
        print(
            "helper canonical model ACL and runtime cache access passed; sibling state is read-only"
        )
    finally:
        shutil.rmtree(installation, ignore_errors=True)
        shutil.rmtree(protected, ignore_errors=True)
        for path in reversed(created):
            try:
                path.rmdir()
            except OSError:
                pass


if __name__ == "__main__":
    main()
