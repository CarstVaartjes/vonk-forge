#!/usr/bin/env python3
"""Executable, fail-closed Spark lifecycle acceptance entry point."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.fspath(Path(__file__).resolve().parents[2]))

from scripts.spark_lifecycle_contract import (
    GATES,
    PHASES,
    ContractError,
    validate_lifecycle,
)

PLATFORMS = ("linux-amd64", "linux-arm64")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
SOURCE_SHA = re.compile(r"[0-9a-f]{40}\Z")
CHANNEL = re.compile(r"(?:dev|stable)\Z")
VERSION = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:[+~][0-9A-Za-z.+~-]+)?\Z")
BASELINE_VERSION = re.compile(
    r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"~acceptance\.1\+g[0-9a-f]{12}\Z"
)
PINNED_IMAGE = re.compile(
    r"[a-z0-9][a-z0-9./_-]*:[A-Za-z0-9][A-Za-z0-9._-]*@sha256:[0-9a-f]{64}\Z"
)


class LifecycleError(RuntimeError):
    """A bounded acceptance failure that contains no credential material."""


def _object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise LifecycleError(f"{label} is invalid")
    return value


def _read_document(path: Path, label: str) -> dict[str, object]:
    try:
        metadata = path.lstat()
        raw = path.read_bytes()
        document = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise LifecycleError(f"{label} is unavailable or invalid") from error
    if path.is_symlink() or not path.is_file() or metadata.st_nlink != 1:
        raise LifecycleError(f"{label} is unsafe")
    return _object(document, label)


def _canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _read_canonical_document(path: Path, label: str) -> dict[str, object]:
    document = _read_document(path, label)
    if path.read_bytes() != _canonical(document):
        raise LifecycleError(f"{label} is not canonical JSON")
    return document


def _atomic_write(path: Path, value: object) -> None:
    parent = path.parent
    try:
        metadata = parent.lstat()
    except OSError as error:
        raise LifecycleError("report directory is unavailable") from error
    if parent.is_symlink() or not parent.is_dir() or metadata.st_nlink < 1:
        raise LifecycleError("report directory is unsafe")
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=parent)
    temporary = Path(name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = -1
            stream.write(_canonical(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _assert_release_identity(
    release: dict[str, object],
    *,
    label: str,
    channel: str,
    generation: str,
    source_sha: str,
) -> None:
    if (
        release.get("schema_version") != 1
        or release.get("channel") != channel
        or release.get("generation") != generation
        or release.get("source_sha") != source_sha
    ):
        raise LifecycleError(f"{label} identity does not match this acceptance run")
    images = _object(release.get("images"), f"{label} image graph")
    if not images or any(
        not isinstance(name, str)
        or not name
        or not isinstance(image, str)
        or PINNED_IMAGE.fullmatch(image) is None
        or any(pointer in image for pointer in (":latest@", ":dev@", ":main@", ":edge@"))
        for name, image in images.items()
    ):
        raise LifecycleError(f"{label} image graph is not immutable")


def _verified_artifact(
    release: dict[str, object],
    *,
    object_root: Path,
    key: str,
    expected_path: str,
    label: str,
) -> str:
    artifacts = _object(release.get("artifacts"), f"{label} artifacts")
    record = _object(artifacts.get(key), f"{label} {key}")
    digest = record.get("sha256")
    size = record.get("size")
    relative = record.get("path")
    if (
        relative != expected_path
        or not isinstance(digest, str)
        or SHA256.fullmatch(digest) is None
        or not isinstance(size, int)
        or isinstance(size, bool)
        or size <= 0
    ):
        raise LifecycleError(f"{label} {key} record is invalid")
    directory_flags = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
    file_flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
    root = object_root if object_root.is_absolute() else Path.cwd() / object_root
    root_parts = root.parts
    relative_parts = Path(expected_path).parts
    if (
        not root.is_absolute()
        or not relative_parts
        or Path(expected_path).is_absolute()
        or any(part in {"", ".", ".."} for part in relative_parts)
    ):
        raise LifecycleError(f"{label} {key} object path is unsafe")
    directory = -1
    descriptor = -1
    try:
        directory = os.open(os.sep, directory_flags)
        for component in (*root_parts[1:], *relative_parts[:-1]):
            child = os.open(component, directory_flags, dir_fd=directory)
            os.close(directory)
            directory = child
        descriptor = os.open(relative_parts[-1], file_flags, dir_fd=directory)
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size != size
        ):
            raise LifecycleError(
                f"{label} {key} object is unsafe or has the wrong size"
            )
        digest_builder = hashlib.sha256()
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                raise LifecycleError(f"{label} {key} object changed while read")
            digest_builder.update(chunk)
            remaining -= len(chunk)
        after = os.fstat(descriptor)
        if any(
            getattr(before, field) != getattr(after, field)
            for field in (
                "st_dev",
                "st_ino",
                "st_nlink",
                "st_size",
                "st_mtime_ns",
                "st_ctime_ns",
            )
        ):
            raise LifecycleError(f"{label} {key} object changed while read")
        observed = digest_builder.hexdigest()
    except OSError as error:
        raise LifecycleError(f"{label} {key} object is unavailable or unsafe") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if directory >= 0:
            os.close(directory)
    if observed != digest:
        raise LifecycleError(f"{label} {key} object digest does not match")
    return digest


def check_publication_graph(arguments: argparse.Namespace) -> dict[str, object]:
    if (
        CHANNEL.fullmatch(arguments.channel) is None
        or VERSION.fullmatch(arguments.version) is None
        or SOURCE_SHA.fullmatch(arguments.source_sha) is None
        or SHA256.fullmatch(arguments.generation) is None
        or arguments.platform not in PLATFORMS
    ):
        raise LifecycleError("publication graph inputs are invalid")
    candidate = _read_document(arguments.candidate_release, "candidate release")
    baseline = _read_document(arguments.baseline_release, "baseline release")
    _assert_release_identity(
        candidate,
        label="candidate release",
        channel=arguments.channel,
        generation=arguments.generation,
        source_sha=arguments.source_sha,
    )
    _assert_release_identity(
        baseline,
        label="baseline release",
        channel=arguments.channel,
        generation=arguments.generation,
        source_sha=arguments.source_sha,
    )
    baseline_version = baseline.get("version")
    if (
        candidate.get("version") != arguments.version
        or not isinstance(baseline_version, str)
        or BASELINE_VERSION.fullmatch(baseline_version) is None
        or baseline.get("acceptance_only") is not True
        or baseline.get("images") != candidate.get("images")
    ):
        raise LifecycleError("candidate and baseline release graphs are inconsistent")
    ordered = subprocess.run(
        ["/usr/bin/dpkg", "--compare-versions", baseline_version, "lt", arguments.version],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if ordered.returncode != 0:
        raise LifecycleError("baseline version is not strictly lower than candidate")

    selected: dict[str, str] = {}
    packages: dict[str, dict[str, str]] = {}
    for platform in PLATFORMS:
        candidate_digest = _verified_artifact(
            candidate,
            object_root=arguments.object_root,
            key=f"agent-package-{platform}",
            expected_path=(
                f"artifacts/{arguments.channel}/releases/{arguments.generation}/"
                f"spark/current/{platform}/vonk-forge-agent.deb"
            ),
            label="candidate release",
        )
        baseline_digest = _verified_artifact(
            baseline,
            object_root=arguments.object_root,
            key=f"agent-package-{platform}",
            expected_path=(
                f"artifacts/{arguments.channel}/releases/{arguments.generation}/"
                f"acceptance-baseline/spark/current/{platform}/vonk-forge-agent.deb"
            ),
            label="baseline release",
        )
        if candidate_digest == baseline_digest:
            raise LifecycleError(f"{platform} candidate and baseline package digests match")
        packages[platform] = {
            "baseline_sha256": baseline_digest,
            "candidate_sha256": candidate_digest,
        }
        if platform == arguments.platform:
            selected = {
                "baseline_package_sha256": baseline_digest,
                "candidate_package_sha256": candidate_digest,
            }
    return {
        **selected,
        "baseline_version": baseline_version,
        "candidate_version": arguments.version,
        "channel": arguments.channel,
        "generation": arguments.generation,
        "images_sha256": hashlib.sha256(_canonical(candidate["images"])).hexdigest(),
        "packages": packages,
        "platform": arguments.platform,
        "schema_version": 1,
        "source_sha": arguments.source_sha,
        "verified_platforms": list(PLATFORMS),
    }


def emit_report(arguments: argparse.Namespace) -> None:
    if (
        CHANNEL.fullmatch(arguments.channel) is None
        or VERSION.fullmatch(arguments.version) is None
        or SOURCE_SHA.fullmatch(arguments.source_sha) is None
        or SHA256.fullmatch(arguments.generation) is None
        or arguments.run_id <= 0
        or arguments.platform not in PLATFORMS
    ):
        raise LifecycleError("report identity is invalid")
    evidence = _read_canonical_document(arguments.evidence, "lifecycle evidence")
    if set(evidence) != {
        "channel",
        "completed_phases",
        "generation",
        "platform",
        "proof",
        "run_id",
        "schema_version",
        "source_sha",
        "version",
    } or any(
        evidence.get(name) != expected
        for name, expected in {
            "schema_version": 1,
            "channel": arguments.channel,
            "version": arguments.version,
            "source_sha": arguments.source_sha,
            "generation": arguments.generation,
            "run_id": arguments.run_id,
            "platform": arguments.platform,
            "completed_phases": PHASES[arguments.platform],
        }.items()
    ):
        raise LifecycleError("lifecycle evidence is incomplete or belongs to another run")
    lifecycle = {
        "completed_phases": evidence["completed_phases"],
        "proof": evidence["proof"],
    }
    try:
        validate_lifecycle(
            lifecycle,
            platform=arguments.platform,
            channel=arguments.channel,
            version=arguments.version,
            source_sha=arguments.source_sha,
            generation=arguments.generation,
        )
    except ContractError as error:
        raise LifecycleError(str(error)) from error
    _atomic_write(
        arguments.output,
        {
            "channel": arguments.channel,
            "gates": GATES[arguments.platform],
            "generation": arguments.generation,
            "lifecycle": lifecycle,
            "platform": arguments.platform,
            "run_id": arguments.run_id,
            "schema_version": 2,
            "source_sha": arguments.source_sha,
            "status": "passed",
            "version": arguments.version,
        },
    )


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    graph = commands.add_parser("check-publication-graph")
    graph.add_argument("--candidate-release", type=Path, required=True)
    graph.add_argument("--baseline-release", type=Path, required=True)
    graph.add_argument("--object-root", type=Path, required=True)
    graph.add_argument("--channel", required=True)
    graph.add_argument("--version", required=True)
    graph.add_argument("--source-sha", required=True)
    graph.add_argument("--generation", required=True)
    graph.add_argument("--platform", required=True)
    report = commands.add_parser("emit-report")
    report.add_argument("--evidence", type=Path, required=True)
    report.add_argument("--output", type=Path, required=True)
    report.add_argument("--channel", required=True)
    report.add_argument("--version", required=True)
    report.add_argument("--source-sha", required=True)
    report.add_argument("--generation", required=True)
    report.add_argument("--run-id", type=int, required=True)
    report.add_argument("--platform", required=True)
    return parser.parse_args()


def main() -> int:
    arguments = _arguments()
    try:
        if arguments.command == "check-publication-graph":
            result = check_publication_graph(arguments)
        elif arguments.command == "emit-report":
            emit_report(arguments)
            return 0
        else:
            raise LifecycleError("lifecycle command is invalid")
    except LifecycleError as error:
        print(f"Spark lifecycle failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
