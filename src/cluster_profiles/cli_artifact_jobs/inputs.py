"""Reading, checking and delivering a job's local input files."""

from __future__ import annotations

import argparse
import hashlib
import os
import stat
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import cast

from cluster_profiles.cli_artifact_jobs.client import (
    ArtifactJobClient,
    _recover,
    _transfer_client,
)
from cluster_profiles.cli_artifact_jobs.responses import _declared_files, _job
from cluster_profiles.cli_artifact_jobs.values import _NAME, _integer, _mapping
from cluster_profiles.cli_files import read_json_document
from cluster_profiles.cli_states import preparation
from cluster_profiles.cli_states_generated import READY
from cluster_profiles.control_client.errors import (
    ControlMalformedResponse,
    ControlTransportError,
    LocalFilesystemError,
)
from cluster_profiles.control_client.schema import validate_control_document
from cluster_profiles.generated_control.media_types import canonical_media_types

_CREATE_FIELDS = {
    "interface",
    "parameters",
    "inputs",
    "output_limits",
    "timeout_seconds",
}


_INPUT_FIELDS = {"slot", "name"}


def _read_binding(source: str) -> tuple[dict[str, object], dict[str, Path]]:
    raw = read_json_document(source)
    if not isinstance(raw, dict) or set(raw) != {"create", "input_paths"}:
        raise ValueError(
            "artifact input file must contain exactly create and input_paths"
        )
    create = raw.get("create")
    paths_value = raw.get("input_paths")
    if not isinstance(create, dict) or set(create) - _CREATE_FIELDS:
        raise ValueError("artifact input create object has unknown fields")
    if not isinstance(paths_value, dict):
        raise TypeError("artifact input_paths must map each input name to a local path")
    inputs = create.get("inputs", [])
    if not isinstance(inputs, list) or len(inputs) > 32:
        raise ValueError(
            "artifact job input declarations must be an array of at most 32 files"
        )
    names: set[str] = set()
    for item in inputs:
        if not isinstance(item, dict) or set(item) != _INPUT_FIELDS:
            raise ValueError(
                "each local input declaration must contain exactly slot and name"
            )
        name = item.get("name")
        if not isinstance(name, str) or _NAME.fullmatch(name) is None or name in names:
            raise ValueError("artifact input names must be unique safe file names")
        names.add(name)
        if not isinstance(item.get("slot"), str):
            raise TypeError("artifact input slot must be a string")
    if set(paths_value) != names:
        raise ValueError("input_paths must name every declared input exactly once")
    base = Path.cwd() if source == "-" else Path(source).absolute().parent
    paths: dict[str, Path] = {}
    for name, raw_path in paths_value.items():
        if not isinstance(raw_path, str) or not raw_path or len(raw_path) > 4096:
            raise ValueError(f"local path for {name} must be a non-empty path string")
        local = Path(raw_path)
        paths[name] = local if local.is_absolute() else base / local
    return create, paths


def _prepare_inputs(
    create: Mapping[str, object],
    paths: Mapping[str, Path],
    capabilities: Mapping[str, object],
) -> list[dict[str, object]]:
    transport = _mapping(capabilities.get("transport"), "artifact job capabilities")
    maximum_files = _integer(transport.get("max_input_files"), "input file limit")
    maximum_file_bytes = _integer(
        transport.get("max_input_file_bytes"), "input file byte limit"
    )
    maximum_total_bytes = _integer(
        transport.get("max_input_total_bytes"), "input total byte limit"
    )
    reserved_names_value = transport.get("reserved_input_names")
    if not isinstance(reserved_names_value, list) or any(
        not isinstance(name, str) for name in reserved_names_value
    ):
        raise ControlMalformedResponse("artifact job reserved input names are invalid")
    inputs = create.get("inputs", [])
    if not isinstance(inputs, list) or len(inputs) > maximum_files:
        raise ValueError(
            f"artifact input count exceeds the Controller limit of {maximum_files}"
        )
    declarations: list[dict[str, object]] = []
    total = 0
    for item in inputs:
        if not isinstance(item, dict):
            raise TypeError("artifact input declaration is invalid")
        name = cast(str, item["name"])
        if name in reserved_names_value:
            raise ValueError(
                f"artifact input name is reserved by the Controller: {name}"
            )
        size, digest = _digest_regular_file(paths[name], maximum_file_bytes, name)
        total += size
        declarations.append(
            {
                "slot": item["slot"],
                "name": name,
                "size_bytes": size,
                "sha256": digest,
            }
        )
    if total > maximum_total_bytes:
        raise ValueError(
            f"artifact input bytes {total} exceed the Controller limit of {maximum_total_bytes}"
        )
    return declarations


def _canonical_create(
    create: Mapping[str, object],
    declarations: list[dict[str, object]],
    capabilities: Mapping[str, object],
) -> dict[str, object]:
    body: dict[str, object] = {
        "interface": create.get("interface"),
        "parameters": create.get("parameters", {}),
        "inputs": declarations,
        "output_limits": create.get("output_limits"),
        "timeout_seconds": create.get("timeout_seconds"),
    }
    validated = validate_control_document("ArtifactJobCreate", body)
    transport = _mapping(capabilities.get("transport"), "artifact job capabilities")
    output_limits = _mapping(validated.get("output_limits"), "artifact output limits")
    output_limits = {
        **output_limits,
        "allowed_media_types": canonical_media_types(
            cast(list[str], output_limits["allowed_media_types"])
        ),
    }
    validated["output_limits"] = output_limits
    maximum_timeout = _integer(
        transport.get("max_timeout_seconds"), "job timeout limit"
    )
    timeout = _integer(validated.get("timeout_seconds"), "job timeout")
    if timeout > maximum_timeout:
        raise ValueError(
            f"job timeout {timeout} exceeds the Controller limit of {maximum_timeout}"
        )
    for field, capability in (
        ("max_files", "max_output_files"),
        ("max_file_bytes", "max_output_file_bytes"),
        ("max_total_bytes", "max_output_total_bytes"),
    ):
        value = _integer(output_limits.get(field), f"output {field}")
        maximum = _integer(transport.get(capability), f"{field} capability")
        if value > maximum:
            raise ValueError(
                f"output {field} {value} exceeds the Controller limit of {maximum}"
            )
    return validated


def _digest_regular_file(path: Path, maximum: int, name: str) -> tuple[int, str]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        raise LocalFilesystemError(
            "artifact inputs cannot be opened safely on this platform"
        )
    descriptor = -1
    try:
        descriptor = os.open(path, flags | no_follow)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError(
                f"artifact input {name} must be a regular non-symlink file"
            )
        if before.st_size > maximum:
            raise ValueError(
                f"artifact input {name} is {before.st_size} bytes; the Controller limit is {maximum}"
            )
        digest = hashlib.sha256()
        observed = 0
        while observed <= maximum:
            chunk = os.read(descriptor, min(1024**2, maximum + 1 - observed))
            if not chunk:
                break
            observed += len(chunk)
            digest.update(chunk)
        after = os.fstat(descriptor)
        identity_before = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        identity_after = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if identity_before != identity_after or observed != before.st_size:
            raise ControlTransportError(
                "artifact input observation changed while being read"
            )
        return observed, digest.hexdigest()
    except OSError as error:
        raise LocalFilesystemError(
            f"artifact input {name} must be a readable regular non-symlink file"
        ) from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _deliver_inputs(
    args: argparse.Namespace,
    client: ArtifactJobClient,
    job: dict[str, object],
    declarations: list[dict[str, object]],
    paths: dict[str, Path],
    quote: Callable[[str], str],
) -> dict[str, object]:
    transfer = _transfer_client(client)
    uploaded = cast(list[dict[str, object]], args.artifact_job_uploaded)
    uploaded_by_name = _declared_files(job.get("input_files"), "uploaded inputs")
    # The Controller derived each input's media type; the upload carries it.
    accepted_by_name = _declared_files(job.get("input_declarations"), "declarations")
    for local in declarations:
        name = cast(str, local["name"])
        declaration = accepted_by_name.get(name)
        if (
            declaration is None
            or {key: value for key, value in declaration.items() if key != "media_type"}
            != local
        ):
            raise ControlMalformedResponse(
                "artifact job declarations differ from the requested inputs"
            )
        current = uploaded_by_name.get(name)
        if current == declaration:
            continue
        path = f"/api/artifact-jobs/{quote(cast(str, job['id']))}/inputs/{quote(name)}"
        identifier = cast(str, job["id"])

        def send(
            timeout: float,
            path=path,
            name=name,
            declaration=declaration,
            identifier=identifier,
        ):
            accepted = _job(
                transfer.upload_file(
                    path,
                    paths[name],
                    media_type=cast(str, declaration["media_type"]),
                    expected_sha256=cast(str, declaration["sha256"]),
                    expected_size=cast(int, declaration["size_bytes"]),
                    timeout_seconds=timeout,
                ),
                expected_id=identifier,
            )
            files = _declared_files(accepted.get("input_files"), "uploaded inputs")
            if files.get(name) != declaration:
                raise ControlMalformedResponse(
                    "artifact upload acknowledgement is incomplete"
                )
            return accepted

        def observe(
            timeout: float, name=name, declaration=declaration, identifier=identifier
        ):
            observed = _job(
                client.request(
                    "GET",
                    f"/api/artifact-jobs/{quote(identifier)}",
                    timeout_seconds=timeout,
                ),
                expected_id=identifier,
            )
            files = _declared_files(observed.get("input_files"), "uploaded inputs")
            return observed if files.get(name) == declaration else None

        job = _recover(client, send, observe)
        uploaded.append({"name": name, "sha256": declaration["sha256"]})
        uploaded_by_name = _declared_files(job.get("input_files"), "uploaded inputs")
    if preparation(job) == READY or (
        declarations
        and all(
            uploaded_by_name.get(cast(str, item["name"]))
            == accepted_by_name.get(cast(str, item["name"]))
            for item in declarations
        )
        and preparation(job) is None
    ):
        return job
    # Finalization is idempotent at the owner. Its lost reply is observed; a
    # concurrent submit is accepted as evidence that preparation completed.
    identifier = cast(str, job["id"])

    def finalize(timeout: float):
        return _job(
            client.request(
                "POST",
                f"/api/artifact-jobs/{quote(identifier)}/finalize",
                timeout_seconds=timeout,
            ),
            expected_id=identifier,
        )

    def observe_finalization(timeout: float):
        observed = _job(
            client.request(
                "GET",
                f"/api/artifact-jobs/{quote(identifier)}",
                timeout_seconds=timeout,
            ),
            expected_id=identifier,
        )
        return (
            observed
            if preparation(observed) == READY
            or observed.get("operation_id") is not None
            else None
        )

    return _recover(client, finalize, observe_finalization)
