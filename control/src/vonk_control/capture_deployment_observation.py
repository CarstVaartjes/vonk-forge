"""Capture Controller identity from accepted release and projected Docker data.

After authorized redeployment reports the API healthy, the host coordinator
passes the accepted ``release.json`` and detached ``release.sig`` plus only
these Docker fields on stdin: container ID, image ID, configured image,
container hostname, running state, health state, image ID, and repository
digests. Do not pass or persist full ``docker inspect`` documents because
container configuration includes secret environment values.

The packaged entry point is::

    docker compose exec -T control-api \
        python -m vonk_control.capture_deployment_observation

The collector verifies the existing installer signing key and checks those
host observations against this running container's actual hostname and
embedded build metadata. It writes the Controller boundary only after every
identity agrees. A verified mismatch clears only that boundary; all other
observation evidence is retained.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import socket
import stat
import sys
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from pydantic import ConfigDict, Field

from cluster_profiles.cli_update import _validate_release

from .deployment_observer import PublishedRelease
from .deployment_provenance import (
    CONTROLLER_BUILD_METADATA,
    _container_id_matches_hostname,
)
from .deployment_provenance_contract import (
    ControllerBuildMetadata,
    DeploymentObservations,
    PlatformObservation,
)
from .strict_json import StrictJSONModel

OBSERVATIONS_ENV = "VONK_DEPLOYMENT_OBSERVATIONS_FILE"
PUBLIC_KEY_PATH = Path("/usr/local/share/vonk-forge/installer-release-public.pem")
MAX_INPUT_BYTES = 2 * 1024 * 1024
MAX_OBSERVATIONS_BYTES = 1024 * 1024
API_REPOSITORY = "ghcr.io/carstvaartjes/vonk-forge-api"
_RELEASE_IMAGE = re.compile(
    re.escape(API_REPOSITORY)
    + r":(?P<tag>[A-Za-z0-9][A-Za-z0-9._-]*)@sha256:(?P<digest>[a-f0-9]{64})\Z"
)
_DEV_IMAGE_SOURCE = re.compile(r"dev-sha-(?P<source>[a-f0-9]{40})\Z")
_CONTAINER_ID = re.compile(r"[a-f0-9]{64}\Z")
_IMAGE_ID = re.compile(r"sha256:[a-f0-9]{64}\Z")


class CaptureModel(StrictJSONModel):
    model_config = ConfigDict(strict=True, extra="forbid")


class ContainerInspection(CaptureModel):
    """Only non-secret fields projected from Docker's container inspection."""

    id: str
    image_id: str
    hostname: str
    configured_image: str
    running: bool
    health: Literal["healthy"]


class ImageInspection(CaptureModel):
    """Only the immutable ID and registry identities projected from Docker."""

    id: str
    repo_digests: list[str] = Field(min_length=1, max_length=64)


class CaptureInput(CaptureModel):
    """Bounded data collected by the host's authorized Docker coordinator."""

    schema_version: Literal[1]
    release_json_base64: str = Field(max_length=1_500_000)
    release_signature_base64: str = Field(max_length=16_384)
    container: ContainerInspection
    image: ImageInspection


class CaptureError(ValueError):
    """Input did not prove this running Controller's accepted release identity."""


def _decode_base64(value: str, label: str) -> bytes:
    try:
        return base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as error:
        raise CaptureError(f"{label} is not valid base64") from error


def _verify_release(
    raw: bytes, signature_bytes: bytes, public_key_path: Path
) -> PublishedRelease:
    try:
        document = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise CaptureError("accepted release JSON is invalid") from error
    if (
        not isinstance(document, dict)
        or raw
        != (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode()
    ):
        raise CaptureError("accepted release JSON is not canonical")
    try:
        release_document = _validate_release(document, raw)
        release = PublishedRelease.model_validate(release_document)
        public_key = serialization.load_pem_public_key(public_key_path.read_bytes())
    except (OSError, ValueError, TypeError) as error:
        raise CaptureError(
            "accepted release contract or signing key is invalid"
        ) from error
    if not isinstance(public_key, rsa.RSAPublicKey) or public_key.key_size not in (
        3072,
        4096,
    ):
        raise CaptureError("installer release public key is invalid")
    try:
        public_key.verify(signature_bytes, raw, padding.PKCS1v15(), hashes.SHA256())
    except InvalidSignature as error:
        raise CaptureError("accepted release signature is invalid") from error
    image = release.images.get("api")
    image_match = _RELEASE_IMAGE.fullmatch(image) if isinstance(image, str) else None
    if image_match is None:
        raise CaptureError("accepted release API image identity is invalid")
    if (
        release.channel == "dev"
        and _DEV_IMAGE_SOURCE.fullmatch(image_match.group("tag")) is None
    ):
        raise CaptureError("accepted development API image source identity is invalid")
    if not re.fullmatch(r"[a-f0-9]{64}", release.generation):
        raise CaptureError("accepted release generation is invalid")
    return release


def _expected_image_source(release: PublishedRelease, expected_image: str) -> str:
    """Return the source that produced the exact API image in this release.

    Development publication may reuse an accepted ancestor image when its
    inputs are unchanged. Its immutable image tag carries that producer source
    (``dev-sha-<commit>``), while the release source identifies the complete
    installer generation. Stable images are version-tagged and are built from
    the release source.
    """

    match = _RELEASE_IMAGE.fullmatch(expected_image)
    if match is None:
        raise CaptureError("accepted release API image identity is invalid")
    if release.channel == "dev":
        source_match = _DEV_IMAGE_SOURCE.fullmatch(match.group("tag"))
        if source_match is None:
            raise CaptureError(
                "accepted development API image source identity is invalid"
            )
        return source_match.group("source")
    return release.source_sha


def _load_observations(path: Path) -> DeploymentObservations:
    parent = path.parent
    try:
        parent_metadata = parent.lstat()
    except FileNotFoundError as error:
        raise CaptureError("deployment observation volume is not mounted") from error
    if not stat.S_ISDIR(parent_metadata.st_mode):
        raise CaptureError("deployment observation directory is unsafe")
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return DeploymentObservations()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_OBSERVATIONS_BYTES:
        raise CaptureError("deployment observations file is unsafe")
    try:
        return DeploymentObservations.model_validate_json(path.read_bytes())
    except (OSError, ValueError) as error:
        raise CaptureError("deployment observations file is invalid") from error


def _write_observations(path: Path, observations: DeploymentObservations) -> None:
    parent = path.parent
    try:
        parent_metadata = parent.lstat()
    except FileNotFoundError as error:
        raise CaptureError("deployment observation volume is not mounted") from error
    if not stat.S_ISDIR(parent_metadata.st_mode):
        raise CaptureError("deployment observation directory is unsafe")
    encoded = observations.model_dump_json(exclude_none=True).encode() + b"\n"
    if len(encoded) > MAX_OBSERVATIONS_BYTES:
        raise CaptureError("deployment observations exceed the size limit")
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".observations-", dir=parent
        )
        temporary = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o644)
            with os.fdopen(descriptor, "wb") as output:
                output.write(encoded)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
            directory_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if temporary.exists():
                temporary.unlink()
    except OSError as error:
        raise CaptureError(
            "deployment observations could not be stored atomically"
        ) from error


def _clear_controller(path: Path) -> None:
    observations = _load_observations(path)
    observations.controller = None
    _write_observations(path, observations)


def capture(
    request: CaptureInput,
    *,
    observation_path: Path,
    build_metadata_path: Path | None = None,
    public_key_path: Path | None = None,
    runtime_hostname: str | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> PlatformObservation:
    """Verify signed release and current Docker identity, then atomically record."""

    build_metadata_path = build_metadata_path or CONTROLLER_BUILD_METADATA
    public_key_path = public_key_path or PUBLIC_KEY_PATH
    release_raw = _decode_base64(request.release_json_base64, "accepted release JSON")
    signature_bytes = _decode_base64(
        request.release_signature_base64, "accepted release signature"
    )
    release = _verify_release(release_raw, signature_bytes, public_key_path)
    expected_image = release.images["api"]
    image_match = _RELEASE_IMAGE.fullmatch(expected_image)
    if image_match is None:
        raise CaptureError("accepted release API image identity is invalid")
    expected_digest = "sha256:" + image_match.group("digest")
    expected_source = _expected_image_source(release, expected_image)

    container = request.container
    image = request.image
    container_id = container.id
    image_id = container.image_id
    hostname = (
        runtime_hostname if runtime_hostname is not None else socket.gethostname()
    )
    if _CONTAINER_ID.fullmatch(
        container_id
    ) is None or not _container_id_matches_hostname(container_id, hostname):
        raise CaptureError(
            "Docker container inspection is not for this running Controller"
        )
    if (
        _IMAGE_ID.fullmatch(image_id) is None
        or image.id != image_id
        or container.hostname != container_id[:12]
        or not container.configured_image.startswith(API_REPOSITORY + ":")
    ):
        raise CaptureError(
            "Docker container and image inspections do not identify one instance"
        )
    if not container.running:
        raise CaptureError("Docker Controller container is not running")
    repository_digests = image.repo_digests
    matching_repository_digest = f"{API_REPOSITORY}@{expected_digest}"
    actual_repository_digests = {
        value
        for value in repository_digests
        if value.startswith(API_REPOSITORY + "@sha256:")
    }

    try:
        build = ControllerBuildMetadata.model_validate_json(
            build_metadata_path.read_bytes()
        )
    except (OSError, ValueError) as error:
        raise CaptureError(
            "running Controller build metadata is missing or invalid"
        ) from error

    if not actual_repository_digests:
        _clear_controller(observation_path)
        raise CaptureError(
            "running Controller image has no matching immutable repository digest"
        )
    if (
        matching_repository_digest not in actual_repository_digests
        or build.source_commit != expected_source
    ):
        _clear_controller(observation_path)
        raise CaptureError(
            "running Controller does not match the accepted signed release"
        )

    observation = PlatformObservation(
        source="Verified accepted release and running Docker Controller instance",
        observed_at=clock(),
        source_commit=build.source_commit,
        image_digest=next(
            value
            for value in actual_repository_digests
            if value == matching_repository_digest
        ).rsplit("@", 1)[1],
        manifest_sha256=hashlib.sha256(release_raw).hexdigest(),
        container_id=container_id,
    )
    observations = _load_observations(observation_path)
    observations.controller = observation
    _write_observations(observation_path, observations)
    return observation


def main() -> int:
    raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        raise SystemExit("deployment capture input exceeds the size limit")
    try:
        request = CaptureInput.model_validate_json(raw)
        observation_path_value = os.environ.get(OBSERVATIONS_ENV)
        if not observation_path_value:
            raise CaptureError(f"{OBSERVATIONS_ENV} is not configured")
        observation = capture(
            request,
            observation_path=Path(observation_path_value),
        )
    except (ValueError, OSError) as error:
        raise SystemExit(str(error)) from error
    print(f"recorded Controller image {observation.image_digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
