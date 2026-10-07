"""Bounded, serialized dependency preparation for local checks."""

from __future__ import annotations

import fcntl
import hashlib
import os
import signal
import subprocess
import sys
import time
import tomllib
from collections.abc import Callable, Sequence
from pathlib import Path

PREPARATION_TIMEOUT = 600


def run_preparation(command: list[str], root: Path, deadline: float) -> None:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("Check environment preparation timed out")
    with subprocess.Popen(command, cwd=root, start_new_session=True) as process:
        try:
            status = process.wait(timeout=remaining)
        except BaseException:
            # Stop descendants too; an orphaned installer must not race a new check.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=30)
            raise
    if status:
        raise subprocess.CalledProcessError(status, command)


def prepare(
    root: Path,
    lock: Path,
    label: str,
    ready: Callable[[], bool],
    commands: Sequence[list[str]],
    *,
    timeout: float = PREPARATION_TIMEOUT,
    completed: Callable[[], object] | None = None,
) -> None:
    deadline = time.monotonic() + timeout
    lock.parent.mkdir(parents=True, exist_ok=True)
    # Keep the inode: unlinking a flock file would allow independent owners.
    # The kernel releases ownership even if a commit process dies.
    with lock.open("a") as stream:
        while True:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(
                        f"Timed out waiting for {label} preparation lock"
                    )
                time.sleep(min(0.1, remaining))
        if ready():
            return
        print(f"Preparing {label} for checks…", file=sys.stderr, flush=True)
        for command in commands:
            run_preparation(command, root, deadline)
        if completed is not None:
            completed()


def ensure_control(root: Path, environment: Path) -> None:
    inputs = [
        root / name
        for name in (
            "control/uv.lock",
            "control/pyproject.toml",
            "agent_protocol/pyproject.toml",
        )
    ]
    inputs.extend(sorted((root / "agent_protocol/src").rglob("*.py")))
    fingerprint = hashlib.sha256(
        b"".join(path.read_bytes() for path in inputs)
    ).hexdigest()
    stamp = environment / ".commit-check-inputs"

    def ready() -> bool:
        if not (environment / "bin/python").is_file() or not stamp.is_file():
            return False
        if stamp.read_text() != fingerprint:
            return False
        packages = tomllib.loads((root / "control/uv.lock").read_text())["package"]
        package = next(
            item for item in packages if item["name"] == "vonk-agent-protocol"
        )
        wheel = root / "control" / package["source"]["path"]
        if not wheel.is_file():
            return False
        digest = "sha256:" + hashlib.sha256(wheel.read_bytes()).hexdigest()
        if not any(pin["hash"] == digest for pin in package["wheels"]):
            return False
        return (
            subprocess.run(
                [
                    str(environment / "bin/python"),
                    "-c",
                    "import ruff, pyright, nodejs_wheel",
                ],
                cwd=root,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=30,
            ).returncode
            == 0
        )

    prepare(
        root,
        root / ".state/commit-checks/control.lock",
        "control environment",
        ready,
        [
            [str(root / "scripts/build-control-wheel")],
            ["uv", "sync", "--project", "control", "--frozen"],
        ],
        completed=lambda: stamp.write_text(fingerprint),
    )


def ensure_web(root: Path) -> None:
    directory = root / "control/web"
    fingerprint = hashlib.sha256(
        b"".join(
            (directory / name).read_bytes()
            for name in ("package.json", "package-lock.json")
        )
    ).hexdigest()
    stamp = directory / "node_modules/.commit-check-inputs"

    def ready() -> bool:
        return (
            all(
                (directory / "node_modules/.bin" / name).exists()
                for name in ("biome", "tsc")
            )
            and stamp.is_file()
            and stamp.read_text() == fingerprint
        )

    prepare(
        root,
        root / ".state/commit-checks/web.lock",
        "TypeScript tools",
        ready,
        [["npm", "ci", "--prefix", "control/web"]],
        completed=lambda: stamp.write_text(fingerprint),
    )


def ensure_catalog(root: Path, library: Path) -> None:
    prepare(
        root,
        library / ".state/check-catalog.lock",
        "recipe catalog",
        lambda: (library / "catalog-index.json").is_file(),
        [[str(root / "scripts/build-recipe-library"), str(library)]],
    )
