"""Bounded, serialized dependency preparation for local checks."""

from __future__ import annotations

import fcntl
import hashlib
import os
import signal
import subprocess
import sys
import time
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


# One virtual environment (the workspace root's .venv) holds every member, so
# control and protocol preparation share one lock file and never sync at once.
ENVIRONMENT_LOCK = ".state/commit-checks/environment.lock"
DEFAULT_ENVIRONMENT = ".venv"


def _fingerprint(root: Path, names: Sequence[str]) -> str:
    inputs = [root / name for name in names]
    return hashlib.sha256(b"".join(path.read_bytes() for path in inputs)).hexdigest()


def _imports(root: Path, environment: Path, modules: str) -> bool:
    return (
        subprocess.run(
            [str(environment / "bin/python"), "-c", f"import {modules}"],
            cwd=root,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=30,
        ).returncode
        == 0
    )


def ensure_control(root: Path, environment: Path) -> None:
    fingerprint = _fingerprint(
        root,
        (
            "uv.lock",
            "pyproject.toml",
            "control/pyproject.toml",
            "nas_setup/pyproject.toml",
            "nas_setup/hatch_build.py",
            "agent_protocol/pyproject.toml",
        ),
    )
    stamp = environment / ".commit-check-inputs"

    def ready() -> bool:
        return (
            (environment / "bin/python").is_file()
            and stamp.is_file()
            and stamp.read_text() == fingerprint
            and _imports(root, environment, "ruff, pyright, nodejs_wheel")
        )

    prepare(
        root,
        root / ENVIRONMENT_LOCK,
        "control environment",
        ready,
        [["uv", "sync", "--project", "control", "--frozen"]],
        completed=lambda: stamp.write_text(fingerprint),
    )


def ensure_web(root: Path) -> None:
    directory = root / "control/web"
    inputs = [
        directory / name
        for name in ("package.json", "package-lock.json")
        if (directory / name).is_file()
    ]
    fingerprint = hashlib.sha256(
        b"".join(path.read_bytes() for path in inputs)
    ).hexdigest()
    stamp = directory / "node_modules/.commit-check-inputs"
    locked = (directory / "package-lock.json").is_file()

    def ready() -> bool:
        tools = all(
            (directory / "node_modules/.bin" / name).exists()
            for name in ("biome", "tsc")
        )
        if not locked:
            # Nothing to install from: tools that are present are the environment.
            return tools
        return tools and stamp.is_file() and stamp.read_text() == fingerprint

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


def ensure_protocol(root: Path, environment: Path) -> None:
    """The protocol member's own dependencies: all the wire exporter imports.

    ``uv run --package`` semantics (inexact): this adds the member to the shared
    environment and never removes what control installed. A bare
    ``uv sync --package`` would uninstall the other members from it.
    """

    fingerprint = _fingerprint(
        root,
        ("uv.lock", "pyproject.toml", "agent_protocol/pyproject.toml"),
    )
    stamp = environment / ".protocol-inputs"

    def ready() -> bool:
        return (
            (environment / "bin/python").is_file()
            and stamp.is_file()
            and stamp.read_text() == fingerprint
            and _imports(root, environment, "jsonschema, pydantic, vonk_agent_protocol")
        )

    prepare(
        root,
        root / ENVIRONMENT_LOCK,
        "protocol environment",
        ready,
        [
            [
                str(root / "scripts/ensure-project-venv"),
                str((root / environment).resolve()),
            ],
            [
                "env",
                f"UV_PROJECT_ENVIRONMENT={environment}",
                "uv",
                "sync",
                "--inexact",
                "--package",
                "vonk-agent-protocol",
                "--frozen",
                "--no-dev",
            ],
        ],
        completed=lambda: stamp.write_text(fingerprint),
    )


def _reexec_in(environment: Path) -> None:
    if Path(sys.prefix).resolve() != environment.resolve():
        # Like ``uv run``: the environment's console scripts (code generators)
        # resolve before any system copy.
        binaries = str(environment / "bin")
        os.execve(
            str(environment / "bin/python"),
            [
                str(environment / "bin/python"),
                str(Path(sys.argv[0]).resolve()),
                *sys.argv[1:],
            ],
            {
                **os.environ,
                "VIRTUAL_ENV": str(environment),
                "PATH": os.pathsep.join(
                    [
                        binaries,
                        *filter(None, os.environ.get("PATH", "").split(os.pathsep)),
                    ]
                ),
            },
        )


def _environment(root: Path) -> Path:
    environment = Path(os.environ.get("UV_PROJECT_ENVIRONMENT", DEFAULT_ENVIRONMENT))
    return environment if environment.is_absolute() else root / environment


def run_in_control(root: Path) -> None:
    """The locked environment is ready before generator imports execute."""

    environment = _environment(root)
    ensure_control(root, environment)
    _reexec_in(environment)


def run_in_protocol(root: Path) -> None:
    """The wire exporters read only the protocol member of the same environment."""

    environment = _environment(root)
    ensure_protocol(root, environment)
    _reexec_in(environment)
