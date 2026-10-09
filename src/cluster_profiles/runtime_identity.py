"""Identity of installed code, independent of any publication pointer."""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from importlib.resources import files
from zipfile import ZipFile


def contract_fingerprint(document: object) -> str:
    return hashlib.sha256(
        json.dumps(
            document, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
    ).hexdigest()


@dataclass(frozen=True)
class RuntimeBuildIdentity:
    source_sha: str | None
    control_contract_sha256: str | None
    worker_contract_sha256: str | None


def verified_wheel_identity(
    archive: ZipFile, *, source_sha: str, version: str
) -> RuntimeBuildIdentity:
    """Observe content fingerprints of a wheel verified by signed ingress.

    Source/version arguments remain descriptive publication context. A missing
    producer stamp cannot invalidate the wheel bytes already accepted by digest.
    """
    try:
        schema = json.loads(
            archive.read("cluster_profiles/schemas/control-openapi.json")
        )
        control = contract_fingerprint(schema)
    except (KeyError, ValueError, OSError):
        control = None
    try:
        value = json.loads(archive.read("cluster_profiles/build-identity.json"))
    except (KeyError, ValueError, OSError):
        value = None
    worker = value.get("worker_contract_sha256") if isinstance(value, dict) else None
    if not isinstance(worker, str) or re.fullmatch(r"[0-9a-f]{64}", worker) is None:
        worker = None
    source = value.get("source_sha") if isinstance(value, dict) else None
    if not isinstance(source, str) or re.fullmatch(r"[0-9a-f]{40}", source) is None:
        source = None
    return RuntimeBuildIdentity(source, control, worker)


def packaged_runtime_identity() -> RuntimeBuildIdentity:
    """An unstamped or damaged package has unknown provenance, never guessed."""
    unknown = RuntimeBuildIdentity(None, None, None)
    try:
        package = files("cluster_profiles")
        value = json.loads(package.joinpath("build-identity.json").read_text())
        schema = json.loads(
            package.joinpath("schemas/control-openapi.json").read_text()
        )
    except (OSError, ValueError):
        return unknown
    if not isinstance(value, dict) or value.get("schema_version") != 2:
        return unknown
    source = value.get("source_sha")
    control = value.get("control_contract_sha256")
    worker = value.get("worker_contract_sha256")
    if not isinstance(source, str) or re.fullmatch(r"[0-9a-f]{40}", source) is None:
        source = None  # history cannot erase valid content compatibility
    if not isinstance(control, str) or control != contract_fingerprint(schema):
        return unknown
    if not isinstance(worker, str) or re.fullmatch(r"[0-9a-f]{64}", worker) is None:
        return unknown
    return RuntimeBuildIdentity(source, control, worker)


def installed_content_identity() -> str | None:
    """Observe installed package bytes within a byte and time budget."""
    deadline = time.monotonic() + 5
    remaining = 64 * 1024 * 1024
    members: list[tuple[str, str]] = []
    pending = [("cluster_profiles", files("cluster_profiles"))]
    try:
        while pending:
            name, member = pending.pop()
            if time.monotonic() >= deadline:
                return None
            if member.is_dir():
                pending.extend(
                    (f"{name}/{child.name}", child)
                    for child in member.iterdir()
                    if child.name != "__pycache__"
                )
            elif member.is_file() and member.name != "build-identity.json":
                with member.open("rb") as source:
                    content = source.read(remaining + 1)
                remaining -= len(content)
                if remaining < 0:
                    return None
                members.append((name, hashlib.sha256(content).hexdigest()))
    except (OSError, ValueError):
        return None
    return contract_fingerprint(sorted(members))


def wheel_content_identity(archive: ZipFile) -> str | None:
    """Compare content without publication provenance or zip metadata."""
    deadline = time.monotonic() + 5
    remaining = 64 * 1024 * 1024
    members: list[tuple[str, str]] = []
    for member in archive.infolist():
        if (
            member.is_dir()
            or not member.filename.startswith("cluster_profiles/")
            or "/__pycache__/" in member.filename
            or member.filename == "cluster_profiles/build-identity.json"
        ):
            continue
        if time.monotonic() >= deadline or member.file_size > remaining:
            return None
        with archive.open(member) as source:
            content = source.read(remaining + 1)
        remaining -= len(content)
        if remaining < 0:
            return None
        members.append((member.filename, hashlib.sha256(content).hexdigest()))
    return contract_fingerprint(sorted(members))
