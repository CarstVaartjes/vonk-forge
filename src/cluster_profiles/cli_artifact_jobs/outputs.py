"""Downloading and verifying a job's result files."""

from __future__ import annotations

import argparse
import stat
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import cast

from cluster_profiles.cli_artifact_jobs.client import (
    ArtifactJobClient,
    _transfer_client,
)
from cluster_profiles.cli_artifact_jobs.responses import _job
from cluster_profiles.cli_artifact_jobs.values import (
    _DIGEST,
    _MEDIA_TYPE,
    _NAME,
    _integer,
    _mapping,
)
from cluster_profiles.cli_json_contract import Word
from cluster_profiles.cli_states import ENDED_WITHOUT_RESULT
from cluster_profiles.cli_states_generated import SUCCEEDED
from cluster_profiles.control_client.errors import (
    ControlMalformedResponse,
    ControlTransportError,
    JobEndedWithoutResult,
    JobNotFinished,
    LocalFilesystemError,
)
from cluster_profiles.control_client.transfers import (
    download_destination as _download_destination,
)
from cluster_profiles.control_client.transfers import local_filesystem_error
from cluster_profiles.control_client.transfers import (
    verified_output as _existing_verified_file,
)


def _output_directory(path: Path, *, create: bool = False) -> Path:
    """The existing output directory; with ``create``, made first (``mkdir -p``).

    A local filesystem problem is the operator's to fix: it is named as such and
    never reported as the control API being unavailable or retried.
    """
    try:
        if create:
            path.mkdir(parents=True, exist_ok=True)
        metadata = path.lstat()
    except OSError as error:
        raise local_filesystem_error(path, error) from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise LocalFilesystemError(
            "artifact output path must be an existing non-symlink directory"
        )
    try:
        return path.resolve(strict=True)
    except OSError as error:
        raise local_filesystem_error(path, error) from error


def _download(
    args: argparse.Namespace,
    client: ArtifactJobClient,
    quote: Callable[[str], str],
    timeout: float,
) -> dict[str, object]:
    args.artifact_job_id = args.job_id
    args.artifact_job_downloaded = []
    result = _job(
        client.request(
            "GET", f"/api/artifact-jobs/{quote(args.job_id)}", timeout_seconds=timeout
        ),
        expected_id=args.job_id,
    )
    if result["state"] != SUCCEEDED:
        state = result["state"]
        if state in ENDED_WITHOUT_RESULT:
            raise JobEndedWithoutResult(
                f"job {args.job_id} ended {state} and has no result files: "
                f"{result.get('status_reason') or 'no reason recorded'}",
                detail=f"vonkctl {Word.JOB} {Word.DETAIL} {args.job_id}",
            )
        follow = f"vonkctl {Word.JOB} {Word.DETAIL} {args.job_id} --follow"
        raise JobNotFinished(
            f"job {args.job_id} is {state} and has no result yet; wait with {follow}",
            follow=follow,
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
    deadline = time.monotonic() + timeout
    directory = _output_directory(args.output, create=True)
    transfer = _transfer_client(client)
    downloaded = cast(list[dict[str, object]], args.artifact_job_downloaded)
    args.artifact_job_download_total = len(declared)
    for item in declared:
        name = cast(str, item["name"])
        media_type = cast(str, item["media_type"])
        size_bytes = cast(int, item["size_bytes"])
        sha256 = cast(str, item["sha256"])
        destination = _download_destination(directory / name, size_bytes, sha256)
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
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ControlTransportError(
                "artifact download observation deadline reached"
            )
        response = transfer.download_file(
            f"/api/artifact-jobs/{quote(args.job_id)}/results/{quote(name)}/{sha256}",
            destination,
            media_type=media_type,
            expected_sha256=sha256,
            expected_size=size_bytes,
            overwrite=False,
            timeout_seconds=remaining,
        )
        downloaded.append(
            {
                "name": name,
                "path": response.get("destination", str(destination)),
                "size_bytes": size_bytes,
                "sha256": sha256,
                "state": "downloaded",
                **(
                    {"transfer": dict(response)}
                    if isinstance(response, Mapping)  # pyright: ignore[reportUnnecessaryIsInstance]  # a Controller response is checked before use
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
