#!/usr/bin/env python3
"""Run the Controller/Spark wire-contract tier in a local Linux container.

``control/tests/test_*_wire_bridge.py`` does not read JSON artifacts: it
*executes* the Rust example probes built by
``scripts/tests/run_agent_wire_contracts.py``, feeds Controller-produced JSON to
the probe's stdin, and parses the probe's stdout. Those probes are Linux-only,
so the whole tier -- Python, uv, cargo and pytest -- has to run inside Linux.
That is what this developer lane does through Docker/OrbStack.

The command it runs is the ``controller-spark-wire`` command from
``.github/workflows/ci.yml``, whitespace for whitespace. The lane refuses to run
when the workflow and this lane drift apart, and refuses to report success when
the container exited zero without the wire suites actually passing.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# --- CI lane contract -------------------------------------------------------
# Everything below mirrors the ``controller-spark-wire`` job in
# .github/workflows/ci.yml. ``check_lane_consistency`` fails the run when the
# workflow and these values disagree, and tests/scripts/test_dev_agent_wire_linux.py
# asserts the same inside the ordinary repository suite so the drift cannot be
# committed.
CI_WORKFLOW = Path(".github/workflows/ci.yml")
CI_JOB = "controller-spark-wire"
CI_RUNNER = "ubuntu:24.04"
# Container base images are pinned by tag *and* index digest. The digest is the
# one `docker buildx imagetools inspect ubuntu:24.04` reported when it was
# recorded; refresh it with a reviewed edit here, in the Dockerfile and in
# docs/local-linux-lane.md.
BASE_IMAGE = (
    "ubuntu:24.04@sha256:"
    "224a1869083a311ef3f13648a154ba79832fbef6364d31493642ca03082da254"
)
RUST_TOOLCHAIN = "1.97.1"
UV_VERSION = "0.12.1"
PYTHON_VERSION = "3.12"
RECIPE_REVISION_FILE = Path("tests/acceptance/recipe-library-revision.txt")
# Whitespace-normalized form of the CI step "Exercise Controller requests and
# Spark results together". This exact string is executed by the lane.
TIER_COMMAND = (
    "uv run --project control --frozen --with-editable . "
    "python scripts/tests/run_agent_wire_contracts.py -- -q"
)

DOCKERFILE = Path("scripts/dev-agent-wire-linux.Dockerfile")
IMAGE_NAME = "vonk-forge-agent-wire-linux"
IMAGE_LABEL = "org.vonk-forge.lane-dockerfile"
CACHE_ENV = "VONK_AGENT_WIRE_CACHE"
DEFAULT_RECIPE_LIBRARY = Path("/opt/vonk-forge-recipes")
CONTAINER_RECIPE_LIBRARY = "/recipe-library"
CONTAINER_WORKDIR = "/work"


class LaneError(RuntimeError):
    """A condition that makes honest local evidence impossible."""


# --- CI workflow parsing ----------------------------------------------------


def workflow_job(text: str, name: str) -> str:
    """Return the YAML block for one job, without a YAML dependency."""
    marker = f"\n  {name}:\n"
    if marker not in text:
        raise LaneError(f"job {name!r} is missing from {CI_WORKFLOW.as_posix()}")
    body = text.split(marker, 1)[1]
    following = re.search(r"\n  [a-z0-9][a-z0-9-]*:\n", body)
    return body if following is None else body[: following.start()]


def run_commands(job: str) -> list[str]:
    """Return every ``run:`` script in a job, whitespace-normalized."""
    lines = job.splitlines()
    commands: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        block = re.match(r"^([ ]*)run:\s*(?:[|>][-+]?)?\s*$", line)
        if block:
            indent = len(block.group(1))
            index += 1
            collected: list[str] = []
            while index < len(lines):
                candidate = lines[index]
                if candidate.strip():
                    candidate_indent = len(candidate) - len(candidate.lstrip())
                    if candidate_indent <= indent:
                        break
                collected.append(candidate.strip())
                index += 1
            commands.append(" ".join(" ".join(collected).split()))
            continue
        inline = re.match(r"^[ ]*run:\s*(\S.*)$", line)
        if inline:
            commands.append(" ".join(inline.group(1).split()))
        index += 1
    return commands


def lane_contract(workflow_text: str) -> dict[str, object]:
    """Extract the facts the local lane must agree with from the CI job."""
    job = workflow_job(workflow_text, CI_JOB)
    tier_commands = [
        command for command in run_commands(job) if "run_agent_wire_contracts.py" in command
    ]
    rust = re.search(r"rustup toolchain install (\S+)", job)
    uv = re.search(r'^\s*version:\s*"([^"]+)"\s*$', job, re.MULTILINE)
    python = re.search(r'^\s*python-version:\s*"([^"]+)"\s*$', job, re.MULTILINE)
    runner = re.search(r"^\s*runs-on:\s*(\S+)\s*$", job, re.MULTILINE)
    revision = re.search(
        r"cat\s+(tests/acceptance/recipe-library-revision\.txt)", job
    )
    return {
        "tier_commands": tier_commands,
        "rust_toolchain": rust.group(1) if rust else None,
        "uv_version": uv.group(1) if uv else None,
        "python_version": python.group(1) if python else None,
        "runner": runner.group(1) if runner else None,
        "recipe_revision_file": revision.group(1) if revision else None,
        "recipe_library_env": "VONK_RECIPE_LIBRARY_ROOT" in job,
    }


def check_lane_consistency(root: Path) -> list[str]:
    """Return human-readable drift problems; an empty list means consistent."""
    problems: list[str] = []
    workflow_path = root / CI_WORKFLOW
    dockerfile_path = root / DOCKERFILE
    try:
        contract = lane_contract(workflow_path.read_text(encoding="utf-8"))
    except (LaneError, OSError) as exc:
        return [f"cannot read the CI lane contract: {exc}"]

    tier_commands = contract["tier_commands"]
    assert isinstance(tier_commands, list)
    if len(tier_commands) != 1:
        problems.append(
            f"{CI_WORKFLOW.as_posix()} job {CI_JOB!r} must run exactly one "
            f"run_agent_wire_contracts.py step, found {len(tier_commands)}"
        )
    elif tier_commands[0] != TIER_COMMAND:
        problems.append(
            "the CI wire command and this lane disagree:\n"
            f"    CI:   {tier_commands[0]}\n"
            f"    lane: {TIER_COMMAND}"
        )

    comparisons = (
        ("Rust toolchain", RUST_TOOLCHAIN, contract["rust_toolchain"]),
        ("uv version", UV_VERSION, contract["uv_version"]),
        ("Python version", PYTHON_VERSION, contract["python_version"]),
        ("runner image", CI_RUNNER.replace(":", "-"), contract["runner"]),
    )
    for label, expected, found in comparisons:
        if found != expected:
            problems.append(
                f"the CI {label} and this lane disagree: CI has {found!r}, "
                f"the lane pins {expected!r}"
            )

    if not contract["recipe_library_env"]:
        problems.append(
            f"{CI_WORKFLOW.as_posix()} job {CI_JOB!r} no longer sets "
            "VONK_RECIPE_LIBRARY_ROOT"
        )
    if contract["recipe_revision_file"] != RECIPE_REVISION_FILE.as_posix():
        problems.append(
            "the CI recipe revision file and this lane disagree: "
            f"CI reads {contract['recipe_revision_file']!r}, the lane reads "
            f"{RECIPE_REVISION_FILE.as_posix()!r}"
        )

    try:
        base = re.search(
            r"^FROM\s+(\S+)\s*$",
            dockerfile_path.read_text(encoding="utf-8"),
            re.MULTILINE,
        )
    except OSError as exc:
        problems.append(f"cannot read {DOCKERFILE.as_posix()}: {exc}")
    else:
        found = base.group(1) if base else None
        if found != BASE_IMAGE:
            problems.append(
                f"{DOCKERFILE.as_posix()} base image {found!r} does not match "
                f"the pinned lane base {BASE_IMAGE!r}"
            )
    if BASE_IMAGE.partition("@")[0] != CI_RUNNER:
        problems.append(
            f"the lane base {BASE_IMAGE!r} no longer tracks the CI runner {CI_RUNNER!r}"
        )
    if re.fullmatch(r"[^@\s]+@sha256:[0-9a-f]{64}", BASE_IMAGE) is None:
        problems.append(f"the lane base {BASE_IMAGE!r} is not pinned by index digest")
    return problems


# --- Host and container plumbing -------------------------------------------


def stream(command: list[str]) -> tuple[int, str]:
    """Run a command, echo its output, and return (returncode, text)."""
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert process.stdout is not None
    lines: list[str] = []
    for line in process.stdout:
        lines.append(line)
        sys.stdout.write(line)
        sys.stdout.flush()
    return process.wait(), "".join(lines)


def capture(command: list[str]) -> tuple[int, str]:
    completed = subprocess.run(
        command, check=False, capture_output=True, text=True, encoding="utf-8"
    )
    return completed.returncode, (completed.stdout + completed.stderr).strip()


def require_linux_engine() -> str:
    """Return a short description of the Linux engine, or explain the refusal."""
    docker = shutil.which("docker")
    if docker is None:
        raise LaneError(
            "docker is not on PATH. This lane only runs in Linux: install "
            "OrbStack (https://orbstack.dev), start it, and run "
            "`docker context use orbstack`."
        )
    code, output = capture([docker, "info", "--format", "{{.OperatingSystem}}\t{{.OSType}}"])
    if code != 0:
        raise LaneError(
            "cannot reach a Docker engine, so the Linux wire tier cannot run.\n"
            "Start OrbStack and select it with `docker context use orbstack`.\n"
            f"docker said:\n{output}"
        )
    operating_system, _, os_type = output.partition("\t")
    if os_type != "linux":
        raise LaneError(
            f"the active Docker engine reports OS type {os_type!r}, not 'linux'; "
            "the wire probes cannot execute outside Linux."
        )
    context_code, context = capture([docker, "context", "show"])
    context = context if context_code == 0 else "unknown"
    return f"{operating_system} via context {context!r}"


def resolve_recipe_library(argument: str | None, allow_any_revision: bool) -> tuple[Path, str]:
    """Return the sibling recipe-library checkout and the pinned revision."""
    candidate = argument or os.environ.get("VONK_RECIPE_LIBRARY_ROOT")
    library = Path(candidate).expanduser() if candidate else DEFAULT_RECIPE_LIBRARY
    if not library.is_dir():
        raise LaneError(
            f"recipe library {library} is not a directory. Set "
            f"VONK_RECIPE_LIBRARY_ROOT or pass --recipe-library."
        )
    library = library.resolve()
    pinned_path = REPO_ROOT / RECIPE_REVISION_FILE
    pinned = pinned_path.read_text(encoding="utf-8").strip()
    code, head = capture(["git", "-C", str(library), "rev-parse", "HEAD"])
    if code != 0:
        raise LaneError(f"{library} is not a git checkout, so it cannot be compared with CI")
    if head != pinned and not allow_any_revision:
        raise LaneError(
            f"recipe library {library} is at {head}, but CI checks out {pinned} "
            f"({RECIPE_REVISION_FILE.as_posix()}). Check out the pinned revision "
            "or pass --any-recipe-revision to record deliberately different evidence."
        )
    status_code, status = capture(["git", "-C", str(library), "status", "--porcelain"])
    dirty = " (dirty working tree)" if status_code == 0 and status else ""
    return library, f"{head}{dirty}"


def cache_root(argument: str | None) -> Path:
    if argument:
        return Path(argument).expanduser().resolve()
    configured = os.environ.get(CACHE_ENV)
    if configured:
        return Path(configured).expanduser().resolve()
    base = Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache")))
    return (base / "vonk-forge" / "agent-wire-linux").resolve()


def image_tag() -> str:
    return f"{IMAGE_NAME}:{RUST_TOOLCHAIN}-uv{UV_VERSION}-py{PYTHON_VERSION}"


def dockerfile_fingerprint(root: Path) -> str:
    return hashlib.sha256((root / DOCKERFILE).read_bytes()).hexdigest()[:12]


def image_is_current(docker: str, fingerprint: str) -> bool:
    code, output = capture(
        [
            docker,
            "image",
            "inspect",
            "--format",
            f'{{{{ index .Config.Labels "{IMAGE_LABEL}" }}}}',
            image_tag(),
        ]
    )
    return code == 0 and output.strip() == fingerprint


def build_image(docker: str, root: Path, fingerprint: str) -> None:
    print(f"lane: building {image_tag()} from {DOCKERFILE.as_posix()}", flush=True)
    with tempfile.TemporaryDirectory(prefix="vonk-wire-lane-") as context:
        code, _ = stream(
            [
                docker,
                "build",
                "--file",
                str(root / DOCKERFILE),
                "--tag",
                image_tag(),
                "--label",
                f"{IMAGE_LABEL}={fingerprint}",
                "--build-arg",
                f"RUST_TOOLCHAIN={RUST_TOOLCHAIN}",
                "--build-arg",
                f"UV_VERSION={UV_VERSION}",
                "--build-arg",
                f"PYTHON_VERSION={PYTHON_VERSION}",
                context,
            ]
        )
    if code != 0:
        raise LaneError(f"could not build the Linux lane image (exit {code})")


def cache_is_empty(cache: Path) -> bool:
    for name in ("cargo", "cargo-target", "uv"):
        path = cache / name
        if path.is_dir() and any(path.iterdir()):
            return False
    return True


def container_command(
    docker: str, root: Path, library: Path, cache: Path
) -> list[str]:
    uid, gid = os.getuid(), os.getgid()
    mounts = {
        str(root): CONTAINER_WORKDIR,
        str(library): CONTAINER_RECIPE_LIBRARY,
        str(cache / "cargo"): "/cache/cargo",
        str(cache / "cargo-target"): "/cache/cargo-target",
        str(cache / "uv"): "/cache/uv",
        str(cache / "venv"): "/cache/venv",
        str(cache / "home"): "/cache/home",
    }
    command = [
        docker,
        "run",
        "--rm",
        "--user",
        f"{uid}:{gid}",
        "--env",
        "HOME=/cache/home",
        "--env",
        "CARGO_HOME=/cache/cargo",
        "--env",
        "CARGO_TARGET_DIR=/cache/cargo-target",
        "--env",
        "UV_CACHE_DIR=/cache/uv",
        "--env",
        "UV_PROJECT_ENVIRONMENT=/cache/venv",
        "--env",
        f"VONK_RECIPE_LIBRARY_ROOT={CONTAINER_RECIPE_LIBRARY}",
        "--workdir",
        CONTAINER_WORKDIR,
    ]
    for host, target in mounts.items():
        suffix = ":ro" if target == CONTAINER_RECIPE_LIBRARY else ""
        command += ["--volume", f"{host}:{target}{suffix}"]
    command += [image_tag(), "bash", "-c", f"set -euo pipefail; {TIER_COMMAND}"]
    return command


# --- Entry point ------------------------------------------------------------


def parse_arguments(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="scripts/dev-agent-wire-linux",
        description=(
            "Run the Controller/Spark wire-contract tier (the CI "
            "controller-spark-wire job) inside a local Linux container."
        ),
    )
    parser.add_argument(
        "--check-parity",
        action="store_true",
        help="only assert that the CI job and this lane still agree, then exit",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the container invocation instead of running it",
    )
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="rebuild the Linux lane image even when it is already current",
    )
    parser.add_argument(
        "--recipe-library",
        metavar="PATH",
        help="sibling recipe-library checkout (default: VONK_RECIPE_LIBRARY_ROOT)",
    )
    parser.add_argument(
        "--any-recipe-revision",
        action="store_true",
        help="allow a recipe library that is not at the revision CI checks out",
    )
    parser.add_argument(
        "--cache-root",
        metavar="PATH",
        help=f"persistent cargo/uv cache directory (default: $XDG_CACHE_HOME or {CACHE_ENV})",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_arguments(sys.argv[1:] if argv is None else argv)

    problems = check_lane_consistency(REPO_ROOT)
    if problems:
        print("lane: refusing to run; the CI job and this lane have drifted:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        print(
            "Update scripts/dev_agent_wire_linux.py, "
            "scripts/dev-agent-wire-linux.Dockerfile and docs/local-linux-lane.md "
            "together with .github/workflows/ci.yml.",
            file=sys.stderr,
        )
        return 1
    if args.check_parity:
        print(f"lane: CI job {CI_JOB} and the local lane agree")
        return 0

    try:
        return run(args)
    except LaneError as exc:
        print(f"lane: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("lane: interrupted", file=sys.stderr)
        return 130


def run(args: argparse.Namespace) -> int:
    started = time.monotonic()
    library, revision = resolve_recipe_library(args.recipe_library, args.any_recipe_revision)
    cache = cache_root(args.cache_root)
    docker = shutil.which("docker")
    fingerprint = dockerfile_fingerprint(REPO_ROOT)

    if args.dry_run:
        if docker is None:
            raise LaneError("docker is not on PATH")
        print(" ".join(container_command(docker, REPO_ROOT, library, cache)))
        return 0

    engine = require_linux_engine()
    print(f"lane: CI job {CI_JOB} on {engine}")
    print(f"lane: recipe library {library} at {revision}")

    populated = not cache_is_empty(cache)
    for name in ("cargo", "cargo-target", "uv", "venv", "home"):
        (cache / name).mkdir(parents=True, exist_ok=True)
    print(
        f"lane: caches at {cache}"
        + ("" if populated else " (empty; this run will populate them)")
    )

    build_started = time.monotonic()
    if args.rebuild or not image_is_current(docker or "docker", fingerprint):
        build_image(docker or "docker", REPO_ROOT, fingerprint)
    build_seconds = time.monotonic() - build_started

    logs = cache / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    log_path = logs / f"run-{time.strftime('%Y%m%dT%H%M%S')}.log"
    print("lane: running the CI wire command: " + TIER_COMMAND)
    command = container_command(docker or "docker", REPO_ROOT, library, cache)
    run_started = time.monotonic()
    code, output = stream(command)
    run_seconds = time.monotonic() - run_started
    log_path.write_text(output, encoding="utf-8")
    elapsed = time.monotonic() - started

    if code != 0:
        print(
            f"lane: the wire tier failed (exit {code}); full output in {log_path}",
            file=sys.stderr,
        )
        return code

    if "no tests ran" in output or not re.search(r"\d+ passed", output):
        raise LaneError(
            "the container exited 0 but no wire-contract tests reported as passed; "
            f"this is not evidence. Full output in {log_path}"
        )

    print(
        f"lane: passed in {elapsed:.0f}s total "
        f"(image {build_seconds:.0f}s, container {run_seconds:.0f}s); log {log_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
