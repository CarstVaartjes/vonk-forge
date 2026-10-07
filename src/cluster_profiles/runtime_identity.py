"""Identity of installed code, independent of any publication pointer."""

from __future__ import annotations

import hashlib
import json
import re
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
    """Verify the identity against the signed descriptor and the shipped contract."""
    value = json.loads(archive.read("cluster_profiles/build-identity.json"))
    schema = json.loads(archive.read("cluster_profiles/schemas/control-openapi.json"))
    worker = value.get("worker_contract_sha256") if isinstance(value, dict) else None
    control = contract_fingerprint(schema)
    if not isinstance(worker, str) or re.fullmatch(r"[0-9a-f]{64}", worker) is None:
        raise ValueError("CLI worker contract fingerprint is invalid")
    if value != {
        "schema_version": 2,
        "source_sha": source_sha,
        "release_version": version,
        "control_contract_sha256": control,
        "worker_contract_sha256": worker,
    }:
        raise ValueError(
            "CLI package identity differs from signed release or packaged contract"
        )
    return RuntimeBuildIdentity(source_sha, control, worker)


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
    if (
        (
            source is not None
            and (
                not isinstance(source, str)
                or re.fullmatch(r"[0-9a-f]{40}", source) is None
            )
        )
        or not isinstance(control, str)
        or control != contract_fingerprint(schema)
    ):
        return unknown
    if not isinstance(worker, str) or re.fullmatch(r"[0-9a-f]{64}", worker) is None:
        return unknown
    return RuntimeBuildIdentity(source, control, worker)
