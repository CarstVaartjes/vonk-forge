"""The unsigned payload of an installer candidate generation.

Two candidates built from the same accepted artifacts differ only in what
publication adds: the source commit and generation they are published as, the
paths and bootstraps derived from those, the installer key's signatures, and
the release key's trust material inside each agent package. The payload digest
leaves out exactly that and keeps everything a Spark or a NAS runs:

- every image by digest;
- every agent package by its unsealed bytes (scripts/seal-agent-deb) and its
  key-independent identity (version, architecture, binary and build digests);
- the setup programs, the NAS setup wheel and the NAS payload byte for byte;
- the CLI wheel by its content without its publication stamp;
- the version, the acceptance baseline version, channel and schema.

Acceptance evidence for one candidate stands for another only when their
payload digests are equal. Every object is read from the candidate's object
root and must match the digest and size its release records.
"""

from __future__ import annotations

import hashlib
import json
import runpy
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import NotRequired, TypedDict, cast

ROOT = Path(__file__).resolve().parents[1]
#: Records that are signatures by the installer key: never content.
SIGNATURE_PREFIXES = ("agent-package-signature-", "spark-setup-signature-")
MAX_RELEASE_BYTES = 1024 * 1024


class ArtifactRecord(TypedDict):
    """One artifact as a candidate release records it."""

    path: str
    sha256: str
    size: int
    architecture: NotRequired[str]
    package_version: NotRequired[str]
    target_binary_digest: NotRequired[str]
    target_build_digest: NotRequired[str]


class Release(TypedDict):
    """The parts of a candidate or acceptance baseline release that matter."""

    artifacts: dict[str, ArtifactRecord]
    channel: str
    images: dict[str, str]
    schema_version: int
    version: str


class ArtifactPayload(TypedDict, total=False):
    """What an artifact contributes to the payload."""

    sha256: str
    size: int
    unsealed_sha256: str
    content: str
    architecture: str
    package_version: str
    target_binary_digest: str
    target_build_digest: str


class Payload(TypedDict):
    artifacts: dict[str, ArtifactPayload]
    baseline_artifacts: dict[str, ArtifactPayload]
    baseline_version: str
    channel: str
    images: dict[str, str]
    schema_version: int
    version: str


class PayloadError(ValueError):
    """A candidate whose objects do not match its release."""


def _canonical(value: Payload) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        + "\n"
    ).encode()


def _object(object_root: Path, record: ArtifactRecord) -> Path:
    path = object_root / record["path"]
    if path.is_symlink() or not path.is_file():
        raise PayloadError(f"candidate object {record['path']} is missing")
    raw = path.read_bytes()
    if (
        len(raw) != record["size"]
        or hashlib.sha256(raw).hexdigest() != record["sha256"]
    ):
        raise PayloadError(f"candidate object {record['path']} differs from its record")
    return path


def _unsealed_sha256(package: Path) -> str:
    builder = runpy.run_path(str(ROOT / "scripts/build-agent-deb"))
    with tempfile.TemporaryDirectory(prefix="vonk-payload-") as temporary:
        staged = Path(temporary) / "sealed" / "vonk-forge-agent.deb"
        staged.parent.mkdir()
        staged.write_bytes(package.read_bytes())
        unsealed = builder["unseal"](staged, Path(temporary) / "unsealed")
        return hashlib.sha256(unsealed.read_bytes()).hexdigest()


def _wheel_content(wheel: Path) -> str:
    sys.path.insert(0, str(ROOT / "src"))
    from cluster_profiles.runtime_identity import wheel_content_identity

    with zipfile.ZipFile(wheel) as archive:
        identity = wheel_content_identity(archive)
    if identity is None:
        raise PayloadError("the CLI wheel content cannot be identified")
    return identity


def _artifact(name: str, record: ArtifactRecord, object_root: Path) -> ArtifactPayload:
    path = _object(object_root, record)
    if name.startswith("agent-package-"):
        # Version, architecture and binary identity cannot change with a seal.
        package = ArtifactPayload(unsealed_sha256=_unsealed_sha256(path))
        if "architecture" in record:
            package["architecture"] = record["architecture"]
        if "package_version" in record:
            package["package_version"] = record["package_version"]
        if "target_binary_digest" in record:
            package["target_binary_digest"] = record["target_binary_digest"]
        if "target_build_digest" in record:
            package["target_build_digest"] = record["target_build_digest"]
        return package
    if name == "cli-wheel":
        return ArtifactPayload(content=_wheel_content(path))
    return ArtifactPayload(sha256=record["sha256"], size=record["size"])


def _artifacts(release: Release, object_root: Path) -> dict[str, ArtifactPayload]:
    if not release["artifacts"]:
        raise PayloadError("candidate release names no artifacts")
    return {
        name: _artifact(name, record, object_root)
        for name, record in sorted(release["artifacts"].items())
        if not name.startswith(SIGNATURE_PREFIXES)
    }


def _release(path: Path) -> Release:
    if (
        path.is_symlink()
        or not path.is_file()
        or path.stat().st_size > MAX_RELEASE_BYTES
    ):
        raise PayloadError(f"{path.name} is not a candidate release")
    document = json.loads(path.read_bytes())
    if (
        not isinstance(document, dict)
        or not isinstance(document.get("artifacts"), dict)
        or not isinstance(document.get("images"), dict)
        or not all(isinstance(document.get(key), str) for key in ("channel", "version"))
        or not isinstance(document.get("schema_version"), int)
    ):
        raise PayloadError(f"{path.name} is not a candidate release")
    return cast(Release, document)


def payload(candidate_release: Path, object_root: Path) -> Payload:
    """The payload document of the candidate whose release is at this path."""
    candidate = _release(candidate_release)
    baseline = _release(candidate_release.parent / "acceptance-baseline/release.json")
    if not candidate["images"]:
        raise PayloadError("candidate release names no images")
    return Payload(
        artifacts=_artifacts(candidate, object_root),
        baseline_artifacts=_artifacts(baseline, object_root),
        baseline_version=baseline["version"],
        channel=candidate["channel"],
        images={
            role: image.rsplit("@", 1)[-1]
            for role, image in candidate["images"].items()
        },
        schema_version=candidate["schema_version"],
        version=candidate["version"],
    )


def payload_digest(candidate_release: Path, object_root: Path) -> str:
    return hashlib.sha256(
        _canonical(payload(candidate_release, object_root))
    ).hexdigest()
