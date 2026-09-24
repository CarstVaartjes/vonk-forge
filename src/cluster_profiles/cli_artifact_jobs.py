"""Operator workflow for durable recipe artifact jobs."""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shlex
import stat
import sys
import tempfile
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Protocol, cast, runtime_checkable

from .cli_files import read_json_document
from .control_client import (
    ControlClientError,
    ControlHTTPError,
    ControlMalformedResponse,
    ControlNotFound,
    ControlResponseTooLarge,
    ControlTransportError,
    ControlUnavailable,
    validate_control_document,
)

_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_MEDIA_TYPE = re.compile(
    r"^[a-z0-9][a-z0-9!#$&^_.+-]{0,63}/[a-z0-9][a-z0-9!#$&^_.+-]{0,63}$"
)
_CREATE_FIELDS = {
    "interface",
    "parameters",
    "inputs",
    "output_limits",
    "timeout_seconds",
}
_INPUT_FIELDS = {"slot", "name", "media_type"}
_JOB_TERMINAL_STATES = {"succeeded", "failed", "cancelled", "waiting-for-operator"}


@runtime_checkable
class ArtifactJobClient(Protocol):
    @property
    def request_timeout_seconds(self) -> float: ...

    def request(
        self,
        method: str,
        path: str,
        payload: Mapping[str, object] | None = None,
        *,
        extra_headers: Mapping[str, str] | None = None,
        query: Mapping[str, object] | None = None,
        timeout_seconds: float | None = None,
    ) -> dict[str, object]: ...

    def upload_file(
        self,
        path: str,
        source: Path,
        *,
        media_type: str,
        expected_sha256: str,
        expected_size: int,
    ) -> dict[str, object]: ...

    def download_file(
        self,
        path: str,
        destination: Path,
        *,
        media_type: str,
        expected_sha256: str,
        expected_size: int,
        overwrite: bool,
    ) -> dict[str, object]: ...


def add_artifact_job_commands(
    recipe_actions: Any,
    *,
    add_output: Callable[[argparse.ArgumentParser], None],
    watch_controls: Callable[[argparse.ArgumentParser], None],
) -> None:
    """Attach the current artifact-job leaves below ``recipe job``."""

    job = recipe_actions.add_parser("job", help="Create and retrieve recipe outputs")
    add_output(job)
    actions = job.add_subparsers(dest="recipe_job_action", parser_class=type(job))

    list_jobs = actions.add_parser("list", help="List artifact jobs for a run")
    list_jobs.add_argument("--run", required=True, type=_uuid_argument, metavar="ID")
    add_output(list_jobs)

    create = actions.add_parser(
        "create", help="Create a draft and deliver its declared input files"
    )
    create.add_argument("--run", required=True, type=_uuid_argument, metavar="ID")
    create.add_argument(
        "--file",
        required=True,
        help="Bounded JSON file with a canonical create object and local input paths",
    )
    create.add_argument("--request-key", type=_uuid_argument)
    create.set_defaults(outcome_context="mutation")
    add_output(create)

    upload = actions.add_parser(
        "upload", help="Resume missing inputs on an existing draft"
    )
    upload.add_argument("job_id", type=_uuid_argument, metavar="ID")
    upload.add_argument(
        "--file", required=True, help="Same input binding JSON used at create"
    )
    upload.set_defaults(outcome_context="mutation")
    add_output(upload)

    submit = actions.add_parser("submit", help="Submit a ready draft for execution")
    submit.add_argument("job_id", type=_uuid_argument, metavar="ID")
    submit.add_argument("--request-key", type=_uuid_argument)
    submit.set_defaults(outcome_context="mutation")
    add_output(submit)

    detail = actions.add_parser("detail", help="Inspect or follow one artifact job")
    detail.add_argument("job_id", type=_uuid_argument, metavar="ID")
    detail.add_argument("--follow", action="store_true")
    watch_controls(detail)
    add_output(detail)

    cancel = actions.add_parser(
        "cancel", help="Request cancellation of an artifact job"
    )
    cancel.add_argument("job_id", type=_uuid_argument, metavar="ID")
    cancel.add_argument("--yes", action="store_true", help="Confirm this cancellation")
    cancel.add_argument(
        "--reason",
        default="Operator requested cancellation with vonkctl",
        help="Reason retained with the cancellation request",
    )
    cancel.add_argument("--request-key", type=_uuid_argument)
    cancel.set_defaults(outcome_context="mutation")
    add_output(cancel)

    download = actions.add_parser(
        "download", help="Retrieve verified result files from a completed job"
    )
    download.add_argument("job_id", type=_uuid_argument, metavar="ID")
    download.add_argument("--output", required=True, type=Path, metavar="DIRECTORY")
    add_output(download)


def run_artifact_job(
    args: argparse.Namespace,
    client: ArtifactJobClient,
    request_id_factory: Callable[[], str],
    *,
    request_key: Callable[[argparse.Namespace, Callable[[], str]], str],
    quote: Callable[[str], str],
    poll: Callable[..., dict[str, object]],
) -> dict[str, object]:
    action = getattr(args, "recipe_job_action", None)
    if action == "list":
        response = client.request("GET", f"/api/recipe/runs/{args.run}/artifact-jobs")
        jobs = response.get("jobs")
        if not isinstance(jobs, list):
            raise ControlMalformedResponse("artifact job list is invalid")
        return {
            "jobs": [_job(item, expected_run=args.run) for item in jobs],
        }
    if action == "create":
        return _create(args, client, request_id_factory, request_key, quote)
    if action == "upload":
        return _upload_existing(args, client, quote)
    if action == "submit":
        return _submit(args, client, request_id_factory, request_key, quote)
    if action == "detail":
        path = f"/api/artifact-jobs/{quote(args.job_id)}"
        result = _job(client.request("GET", path), expected_id=args.job_id)
        if not args.follow:
            return result
        return poll(
            client,
            path,
            result,
            args,
            terminal=lambda value: value.get("state") in _JOB_TERMINAL_STATES,
            validate=lambda value: _same_job(value, args.job_id),
        )
    if action == "cancel":
        return _cancel(args, client, request_id_factory, request_key, quote)
    if action == "download":
        return _download(args, client, quote)
    raise ValueError(f"unsupported recipe job action: {action}")


def _create(
    args: argparse.Namespace,
    client: ArtifactJobClient,
    request_id_factory: Callable[[], str],
    request_key: Callable[[argparse.Namespace, Callable[[], str]], str],
    quote: Callable[[str], str],
) -> dict[str, object]:
    binding, paths = _read_binding(args.file)
    capabilities = _capabilities(client)
    declarations = _prepare_inputs(binding, paths, capabilities)
    body = _canonical_create(binding, declarations, capabilities)
    key = _stable_request_key(args, request_id_factory, request_key)
    args.request_key = key
    args.artifact_job_run_id = args.run
    args.artifact_job_binding_file = args.file
    args.artifact_job_uploaded = []
    if not _json_mode(args):
        print(
            f"Request key: {key}\nRecovery: vonkctl recipe job create --run {args.run} "
            f"--file {args.file} --request-key {key}",
            file=sys.stderr,
            flush=True,
        )
    path = f"/api/recipe/runs/{quote(args.run)}/artifact-jobs"
    job = _create_with_reconcile(
        args, client, path, body, key, quote, expected_run=args.run
    )
    if not _json_mode(args):
        print(f"Draft ID: {job['id']}", file=sys.stderr, flush=True)
    args.artifact_job_id = job["id"]
    args.artifact_job_reconcile = (
        f"vonkctl recipe job upload {job['id']} --file {args.file}"
    )
    if job["state"] in {"draft", "ready"}:
        job = _deliver_inputs(args, client, job, declarations, paths, quote)
    return job


def _create_with_reconcile(
    args: argparse.Namespace,
    client: ArtifactJobClient,
    path: str,
    body: dict[str, object],
    key: str,
    quote: Callable[[str], str],
    *,
    expected_run: str,
) -> dict[str, object]:
    headers = {"X-Request-ID": key}
    try:
        return _create_receipt(
            client.request("POST", path, body, extra_headers=headers),
            expected_run=expected_run,
            expected_body=body,
        )
    except (ControlClientError, OSError) as first_error:
        if not _may_have_completed(first_error):
            raise
        lookup = f"/api/artifact-jobs/requests/{quote(key)}"
        try:
            observed = _job(client.request("GET", lookup), expected_run=expected_run)
            _match_input_binding(
                observed,
                body,
                cast(list[dict[str, object]], body.get("inputs", [])),
            )
        except ControlNotFound:
            observed = None
        if observed is not None:
            # Replaying the exact request asks the durable owner to compare the
            # complete original intent, including parameters not echoed in the
            # status projection. A reused key with changed input is refused.
            try:
                return _create_receipt(
                    client.request("POST", path, body, extra_headers=headers),
                    expected_run=expected_run,
                    expected_body=body,
                )
            except (ControlClientError, OSError) as replay_error:
                if not _may_have_completed(replay_error):
                    raise
                args.artifact_job_id = observed["id"]
                raise ControlClientError(
                    "artifact draft was found, but its identical create replay could not be confirmed; "
                    f"retry with --request-key {key}"
                ) from replay_error
        try:
            return _create_receipt(
                client.request("POST", path, body, extra_headers=headers),
                expected_run=expected_run,
                expected_body=body,
            )
        except (ControlClientError, OSError) as replay_error:
            if not _may_have_completed(replay_error):
                raise
            try:
                observed = _job(
                    client.request("GET", lookup), expected_run=expected_run
                )
                _match_input_binding(
                    observed,
                    body,
                    cast(list[dict[str, object]], body.get("inputs", [])),
                )
            except (ControlClientError, OSError):
                raise ControlClientError(
                    f"artifact draft acceptance is unknown; retry with --request-key {key}"
                ) from replay_error
            args.artifact_job_id = observed["id"]
            raise ControlClientError(
                "artifact draft may have been accepted; its create response remains unknown; "
                f"retry with --request-key {key}"
            ) from first_error


def _create_receipt(
    value: Mapping[str, object],
    *,
    expected_run: str,
    expected_body: Mapping[str, object],
) -> dict[str, object]:
    job = _job(value, expected_run=expected_run)
    expected_inputs = expected_body.get("inputs")
    if not isinstance(expected_inputs, list):
        raise ControlMalformedResponse(
            "artifact create request has invalid input declarations"
        )
    _match_input_binding(job, expected_body, expected_inputs)
    if job["state"] not in {
        "draft",
        "ready",
        "queued",
        "running",
        "cancelling",
        "cancelled",
        "succeeded",
        "failed",
        "waiting-for-operator",
    }:
        raise ControlMalformedResponse("artifact create receipt has an invalid state")
    return job


def _upload_existing(
    args: argparse.Namespace, client: ArtifactJobClient, quote: Callable[[str], str]
) -> dict[str, object]:
    binding, paths = _read_binding(args.file)
    capabilities = _capabilities(client)
    declarations = _prepare_inputs(binding, paths, capabilities)
    _canonical_create(binding, declarations, capabilities)
    args.artifact_job_id = args.job_id
    args.artifact_job_binding_file = args.file
    args.artifact_job_uploaded = []
    args.artifact_job_reconcile = (
        f"vonkctl recipe job upload {args.job_id} --file {args.file}"
    )
    job = _job(
        client.request("GET", f"/api/artifact-jobs/{quote(args.job_id)}"),
        expected_id=args.job_id,
    )
    _match_input_binding(job, binding, declarations)
    if job["state"] == "ready":
        return job
    if job["state"] != "draft":
        raise ControlClientError(
            f"artifact job is {job['state']}; its inputs can no longer be changed"
        )
    return _deliver_inputs(args, client, job, declarations, paths, quote)


def _deliver_inputs(
    args: argparse.Namespace,
    client: ArtifactJobClient,
    job: dict[str, object],
    declarations: list[dict[str, object]],
    paths: dict[str, Path],
    quote: Callable[[str], str],
) -> dict[str, object]:
    _match_input_binding(job, None, declarations)
    if job["state"] == "ready":
        return job
    if job["state"] != "draft":
        return job
    transfer = _transfer_client(client)
    uploaded = cast(list[dict[str, object]], args.artifact_job_uploaded)
    uploaded_by_name = _declared_files(job.get("input_files"), "uploaded inputs")
    for declaration in declarations:
        name = cast(str, declaration["name"])
        current = uploaded_by_name.get(name)
        if current is not None:
            if current != declaration:
                raise ControlMalformedResponse(
                    f"artifact job input {name} differs from its declaration"
                )
            continue
        path = f"/api/artifact-jobs/{quote(cast(str, job['id']))}/inputs/{quote(name)}"
        try:
            response = transfer.upload_file(
                path,
                paths[name],
                media_type=cast(str, declaration["media_type"]),
                expected_sha256=cast(str, declaration["sha256"]),
                expected_size=cast(int, declaration["size_bytes"]),
            )
        except (ControlClientError, OSError) as error:
            if not _may_have_completed(error):
                raise
            try:
                observed = _job(
                    client.request(
                        "GET", f"/api/artifact-jobs/{quote(cast(str, job['id']))}"
                    ),
                    expected_id=cast(str, job["id"]),
                )
            except (ControlClientError, OSError):
                args.artifact_job_upload_unknown = name
                raise error
            observed_files = _declared_files(
                observed.get("input_files"), "uploaded inputs"
            )
            accepted = observed_files.get(name)
            if accepted != declaration:
                raise
            job = observed
            uploaded.append({"name": name, "sha256": declaration["sha256"]})
            uploaded_by_name = observed_files
            continue
        job = _job(response, expected_id=cast(str, job["id"]))
        uploaded.append({"name": name, "sha256": declaration["sha256"]})
        uploaded_by_name = _declared_files(job.get("input_files"), "uploaded inputs")
    if job["state"] == "draft":
        response = client.request(
            "POST", f"/api/artifact-jobs/{quote(cast(str, job['id']))}/finalize"
        )
        job = _job(response, expected_id=cast(str, job["id"]))
    if job["state"] not in {"ready", "draft"}:
        raise ControlMalformedResponse(
            "artifact input finalization returned an invalid state"
        )
    return job


def _submit(
    args: argparse.Namespace,
    client: ArtifactJobClient,
    request_id_factory: Callable[[], str],
    request_key: Callable[[argparse.Namespace, Callable[[], str]], str],
    quote: Callable[[str], str],
) -> dict[str, object]:
    key = _stable_request_key(args, request_id_factory, request_key)
    args.request_key = key
    args.artifact_job_id = args.job_id
    command = [
        "vonkctl",
        "recipe",
        "job",
        "submit",
        args.job_id,
        "--request-key",
        key,
    ]
    args.artifact_job_reconcile = shlex.join(command)
    if not _json_mode(args):
        print(
            f"Request key: {key}\nRetry: {args.artifact_job_reconcile}",
            file=sys.stderr,
            flush=True,
        )
    path = f"/api/artifact-jobs/{quote(args.job_id)}/submit"
    headers = {"X-Request-ID": key}
    try:
        job = _job(
            client.request("POST", path, extra_headers=headers),
            expected_id=args.job_id,
            expected_submit_request_id=key,
        )
    except (ControlClientError, OSError) as error:
        if not _may_have_completed(error):
            raise
        observed = _job(
            client.request("GET", f"/api/artifact-jobs/{quote(args.job_id)}"),
            expected_id=args.job_id,
        )
        if isinstance(observed.get("operation_id"), str):
            return _job(
                observed,
                expected_id=args.job_id,
                expected_submit_request_id=key,
            )
        if observed["state"] != "ready":
            raise ControlClientError(
                f"submit response was lost; artifact job is now {observed['state']}"
            ) from error
        job = _job(
            client.request("POST", path, extra_headers=headers),
            expected_id=args.job_id,
            expected_submit_request_id=key,
        )
    if not isinstance(job.get("operation_id"), str):
        raise ControlMalformedResponse(
            "artifact submit receipt has no operation identity"
        )
    return job


def _cancel(
    args: argparse.Namespace,
    client: ArtifactJobClient,
    request_id_factory: Callable[[], str],
    request_key: Callable[[argparse.Namespace, Callable[[], str]], str],
    quote: Callable[[str], str],
) -> dict[str, object]:
    if not args.yes:
        raise ValueError("recipe job cancel requires --yes")
    reason = " ".join(args.reason.split())
    if not reason or len(reason) > 512:
        raise ValueError("cancellation reason must contain 1 to 512 characters")
    key = _stable_request_key(args, request_id_factory, request_key)
    args.request_key = key
    args.artifact_job_id = args.job_id
    args.artifact_job_reconcile = shlex.join(
        [
            "vonkctl",
            "recipe",
            "job",
            "cancel",
            args.job_id,
            "--yes",
            "--reason",
            reason,
            "--request-key",
            key,
        ]
    )
    if not _json_mode(args):
        print(
            f"Request key: {key}\nRetry: {args.artifact_job_reconcile}",
            file=sys.stderr,
            flush=True,
        )
    path = f"/api/artifact-jobs/{quote(args.job_id)}/cancel"
    headers = {"X-Request-ID": key}
    try:
        job = _job(
            client.request("POST", path, {"reason": reason}, extra_headers=headers),
            expected_id=args.job_id,
        )
    except (ControlClientError, OSError) as error:
        if not _may_have_completed(error):
            raise
        observed = _job(
            client.request("GET", f"/api/artifact-jobs/{quote(args.job_id)}"),
            expected_id=args.job_id,
        )
        evidence = observed.get("result_evidence")
        accepted = (
            observed["state"] in {"cancelling", "cancelled"}
            and isinstance(evidence, Mapping)
            and evidence.get("cancel_request_id") == key
            and evidence.get("cancel_reason") == reason
        )
        if accepted:
            return observed
        if observed["state"] in {"succeeded", "failed", "cancelled"}:
            raise ControlClientError(
                f"cancellation response was lost; artifact job settled as {observed['state']}"
            ) from error
        job = _job(
            client.request("POST", path, {"reason": reason}, extra_headers=headers),
            expected_id=args.job_id,
        )
    evidence = job.get("result_evidence")
    if (
        job["state"] not in {"cancelling", "cancelled"}
        or not isinstance(evidence, Mapping)
        or evidence.get("cancel_request_id") != key
        or evidence.get("cancel_reason") != reason
    ):
        raise ControlMalformedResponse(
            "artifact cancellation receipt does not identify this request"
        )
    return job


def _download(
    args: argparse.Namespace, client: ArtifactJobClient, quote: Callable[[str], str]
) -> dict[str, object]:
    args.artifact_job_id = args.job_id
    args.artifact_job_downloaded = []
    result = _job(
        client.request("GET", f"/api/artifact-jobs/{quote(args.job_id)}/result"),
        expected_id=args.job_id,
    )
    if result["state"] != "succeeded":
        raise ControlClientError(
            f"artifact job result is unavailable while its state is {result['state']}"
        )
    digest = result.get("output_manifest_sha256")
    outputs = result.get("output_files")
    if not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None:
        raise ControlMalformedResponse("artifact result manifest digest is invalid")
    if not isinstance(outputs, list):
        raise ControlMalformedResponse("artifact job result files are invalid")
    limits = _mapping(result.get("output_limits"), "artifact output limits")
    maximum_files = _integer(limits.get("max_files"), "artifact output file limit")
    maximum_file_bytes = _integer(
        limits.get("max_file_bytes"), "artifact output file byte limit"
    )
    maximum_total_bytes = _integer(
        limits.get("max_total_bytes"), "artifact output total byte limit"
    )
    allowed_media_types = limits.get("allowed_media_types")
    if not isinstance(allowed_media_types, list) or any(
        not isinstance(media_type, str) for media_type in allowed_media_types
    ):
        raise ControlMalformedResponse("artifact output media type limits are invalid")
    if len(outputs) > maximum_files:
        raise ControlMalformedResponse(
            "artifact result exceeds its declared file limit"
        )
    names: set[str] = set()
    total = 0
    declared: list[dict[str, object]] = []
    for item in outputs:
        if not isinstance(item, dict):
            raise ControlMalformedResponse("artifact result file metadata is invalid")
        name = item.get("name")
        media_type = item.get("media_type")
        size_bytes = item.get("size_bytes")
        sha256 = item.get("sha256")
        if (
            not isinstance(name, str)
            or _NAME.fullmatch(name) is None
            or name == "manifest.json"
            or not isinstance(media_type, str)
            or _MEDIA_TYPE.fullmatch(media_type) is None
            or media_type not in allowed_media_types
            or type(size_bytes) is not int
            or not 0 <= size_bytes <= maximum_file_bytes
            or not isinstance(sha256, str)
            or _DIGEST.fullmatch(sha256) is None
            or name in names
        ):
            raise ControlMalformedResponse("artifact result file metadata is invalid")
        names.add(name)
        total += size_bytes
        declared.append(item)
    if total > maximum_total_bytes:
        raise ControlMalformedResponse(
            "artifact result exceeds its declared byte limit"
        )
    capabilities = _capabilities(client)
    transport = _mapping(capabilities.get("transport"), "artifact job capabilities")
    if (
        len(declared) > _integer(transport.get("max_output_files"), "output file limit")
        or any(
            cast(int, item["size_bytes"])
            > _integer(transport.get("max_output_file_bytes"), "output byte limit")
            for item in declared
        )
        or total
        > _integer(transport.get("max_output_total_bytes"), "output total byte limit")
    ):
        raise ControlMalformedResponse(
            "artifact result exceeds Controller transfer limits"
        )
    directory = _output_directory(args.output)
    result_names = [cast(str, item["name"]) for item in declared]
    _reject_output_name_collisions(directory, result_names)
    transfer = _transfer_client(client)
    downloaded = cast(list[dict[str, object]], args.artifact_job_downloaded)
    args.artifact_job_download_total = len(declared)
    for item in declared:
        name = cast(str, item["name"])
        media_type = cast(str, item["media_type"])
        size_bytes = cast(int, item["size_bytes"])
        sha256 = cast(str, item["sha256"])
        destination = directory / name
        if _existing_verified_file(destination, size_bytes, sha256):
            downloaded.append(
                {
                    "name": name,
                    "path": str(destination),
                    "size_bytes": size_bytes,
                    "sha256": sha256,
                    "state": "reused",
                }
            )
            continue
        response = transfer.download_file(
            f"/api/artifact-jobs/{quote(args.job_id)}/results/{quote(name)}/{sha256}",
            destination,
            media_type=media_type,
            expected_sha256=sha256,
            expected_size=size_bytes,
            overwrite=False,
        )
        downloaded.append(
            {
                "name": name,
                "path": str(destination),
                "size_bytes": size_bytes,
                "sha256": sha256,
                "state": "downloaded",
                **(
                    {"transfer": dict(response)}
                    if isinstance(response, Mapping)
                    else {}
                ),
            }
        )
    return {
        "job_id": args.job_id,
        "state": result["state"],
        "output_manifest_sha256": digest,
        "files": downloaded,
        "total_bytes": total,
    }


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
                "each local input declaration must contain exactly slot, name, and media_type"
            )
        name = item.get("name")
        if not isinstance(name, str) or _NAME.fullmatch(name) is None or name in names:
            raise ValueError("artifact input names must be unique safe file names")
        names.add(name)
        if not isinstance(item.get("slot"), str) or not isinstance(
            item.get("media_type"), str
        ):
            raise TypeError("artifact input slot and media_type must be strings")
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
        media_type = cast(str, item["media_type"])
        if _MEDIA_TYPE.fullmatch(media_type) is None:
            raise ValueError(f"artifact input {name} has an invalid media type")
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
                "media_type": media_type,
                "size_bytes": size,
                "sha256": digest,
            }
        )
    if total > maximum_total_bytes:
        raise ValueError(
            f"artifact input bytes {total} exceed the Controller limit of {maximum_total_bytes}"
        )
    storage = _mapping(capabilities.get("storage"), "artifact storage capabilities")
    remaining = _integer(
        storage.get("remaining_bytes"), "artifact storage remaining bytes"
    )
    if total > remaining:
        raise ValueError(
            f"artifact input bytes {total} exceed current Controller storage capacity {remaining}"
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
        raise ControlClientError(
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
            raise ValueError(f"artifact input {name} changed while it was being read")
        return observed, digest.hexdigest()
    except OSError as error:
        raise ControlClientError(
            f"artifact input {name} must be a readable regular non-symlink file"
        ) from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _capabilities(client: ArtifactJobClient) -> dict[str, object]:
    value = client.request("GET", "/api/artifact-jobs/capabilities")
    return validate_control_document("ArtifactJobCapabilitiesResponse", value)


def _job(
    value: Mapping[str, object],
    *,
    expected_id: str | None = None,
    expected_run: str | None = None,
    expected_submit_request_id: str | None = None,
) -> dict[str, object]:
    job = validate_control_document("ArtifactJobResponse", dict(value))
    identifier = job.get("id")
    run_id = job.get("run_id")
    if not isinstance(identifier, str) or _UUID.fullmatch(identifier) is None:
        raise ControlMalformedResponse("artifact job response has an invalid identity")
    if expected_id is not None and identifier != expected_id:
        raise ControlMalformedResponse("artifact job response identifies another job")
    if expected_run is not None and run_id != expected_run:
        raise ControlMalformedResponse("artifact job response identifies another run")
    operation_id = job.get("operation_id")
    submit_request_id = job.get("submit_request_id")
    if (operation_id is None) != (submit_request_id is None):
        raise ControlMalformedResponse("artifact job submission identity is incomplete")
    if (
        expected_submit_request_id is not None
        and submit_request_id != expected_submit_request_id
    ):
        raise ControlMalformedResponse(
            "artifact job response identifies another submit request"
        )
    return job


def _same_job(value: Mapping[str, object], expected_id: str) -> None:
    _job(value, expected_id=expected_id)


def _match_input_binding(
    job: Mapping[str, object],
    create: Mapping[str, object] | None,
    declarations: list[dict[str, object]],
) -> None:
    actual = job.get("input_declarations")
    expected_by_name = {cast(str, item["name"]): item for item in declarations}
    if not isinstance(actual, (list, tuple)):
        raise ControlMalformedResponse("artifact job input declarations are invalid")
    actual_by_name = _declared_files(actual, "input declarations")
    if actual_by_name != expected_by_name:
        raise ControlClientError(
            "local input files do not match this artifact draft's declarations"
        )
    if create is not None:
        for key, value in (
            ("interface", create.get("interface")),
            ("output_limits", create.get("output_limits")),
            ("timeout_seconds", create.get("timeout_seconds")),
        ):
            if job.get(key) != value:
                raise ControlClientError(
                    f"local create settings do not match the artifact draft's {key}"
                )


def _declared_files(value: object, label: str) -> dict[str, dict[str, object]]:
    if not isinstance(value, (list, tuple)):
        raise ControlMalformedResponse(f"artifact job {label} are invalid")
    result: dict[str, dict[str, object]] = {}
    for item in value:
        if not isinstance(item, Mapping):
            raise ControlMalformedResponse(f"artifact job {label} are invalid")
        name = item.get("name")
        if not isinstance(name, str) or name in result:
            raise ControlMalformedResponse(f"artifact job {label} have invalid names")
        result[name] = dict(item)
    return result


def _existing_verified_file(
    path: Path, expected_size: int, expected_sha256: str
) -> bool:
    try:
        before = path.lstat()
    except FileNotFoundError:
        return False
    except OSError as error:
        raise ControlClientError(
            f"cannot inspect existing output {path.name}"
        ) from error
    if not stat.S_ISREG(before.st_mode):
        raise ControlClientError(f"existing output {path.name} is not a regular file")
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        raise ControlClientError(
            "existing outputs cannot be checked safely on this platform"
        )
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | no_follow,
        )
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or opened.st_size != expected_size:
            raise ControlClientError(
                f"existing output {path.name} does not match the result manifest"
            )
        digest = hashlib.sha256()
        observed = 0
        while observed <= expected_size:
            chunk = os.read(descriptor, min(1024**2, expected_size + 1 - observed))
            if not chunk:
                break
            observed += len(chunk)
            digest.update(chunk)
        if observed != expected_size or digest.hexdigest() != expected_sha256:
            raise ControlClientError(
                f"existing output {path.name} does not match the result manifest"
            )
        after = os.fstat(descriptor)
        if (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mtime_ns,
            opened.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise ControlClientError(
                f"existing output {path.name} changed while it was checked"
            )
        return True
    except FileNotFoundError:
        return False
    except OSError as error:
        raise ControlClientError(
            f"cannot verify existing output {path.name}"
        ) from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _output_directory(path: Path) -> Path:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise ControlClientError(f"output directory is unavailable: {path}") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ControlClientError(
            "artifact output path must be an existing non-symlink directory"
        )
    try:
        return path.resolve(strict=True)
    except OSError as error:
        raise ControlClientError("artifact output directory is unavailable") from error


def _reject_output_name_collisions(directory: Path, names: list[str]) -> None:
    if len(names) < 2:
        return
    try:
        with tempfile.TemporaryDirectory(
            prefix=".vonk-name-check-", dir=directory
        ) as temporary:
            probe_directory = Path(temporary)
            for name in names:
                try:
                    descriptor = os.open(
                        probe_directory / name,
                        os.O_CREAT
                        | os.O_EXCL
                        | os.O_WRONLY
                        | getattr(os, "O_CLOEXEC", 0)
                        | getattr(os, "O_NOFOLLOW", 0),
                        0o600,
                    )
                except FileExistsError as error:
                    raise ControlClientError(
                        f"artifact result names alias on this filesystem: {name} and another output"
                    ) from error
                os.close(descriptor)
    except ControlClientError:
        raise
    except OSError as error:
        raise ControlClientError(
            "cannot check artifact output filename collisions safely"
        ) from error


def _transfer_client(client: ArtifactJobClient) -> ArtifactJobClient:
    if not callable(getattr(client, "upload_file", None)) or not callable(
        getattr(client, "download_file", None)
    ):
        raise ControlClientError("artifact byte transfer is unavailable in this client")
    return client


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ControlMalformedResponse(f"{label} are invalid")
    return value


def _integer(value: object, label: str) -> int:
    if type(value) is not int or value < 0:
        raise ControlMalformedResponse(f"{label} is invalid")
    return value


def _json_mode(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "global_json", False) or getattr(args, "json", False))


def _uuid_argument(value: str) -> str:
    try:
        normalized = str(uuid.UUID(value))
    except ValueError:
        raise argparse.ArgumentTypeError(
            "artifact job selector must be a UUID"
        ) from None
    if _UUID.fullmatch(normalized) is None:
        raise argparse.ArgumentTypeError(
            "artifact job selector must be a supported UUID"
        )
    return normalized


def _stable_request_key(
    args: argparse.Namespace,
    factory: Callable[[], str],
    resolver: Callable[[argparse.Namespace, Callable[[], str]], str],
) -> str:
    value = resolver(args, factory)
    if _UUID.fullmatch(value) is None:
        raise ValueError("request key must be a version 1–5 UUID")
    return value


def _may_have_completed(error: BaseException) -> bool:
    if isinstance(error, (ControlTransportError, ControlUnavailable, OSError)):
        return True
    if isinstance(error, ControlHTTPError):
        return error.status_code >= 500
    if isinstance(error, (ControlMalformedResponse, ControlResponseTooLarge)):
        status = error.context.http_status if error.context is not None else None
        return status is None or not 400 <= status < 500
    return False


__all__ = ["add_artifact_job_commands", "run_artifact_job"]
