"""The Controller's layered runtime image store.

Runtime images live in one content-addressed OCI layout,
``<artifact root>/image-cache/oci``. Images share layers there: a recipe
update adds only its changed layers, and the agent site serves the layout
read-only and by digest to Sparks, which ``docker pull`` only what they lack.

Bytes are verified once, where they enter. A registry image (a CI-built
prebuilt image) is copied by digest with ``--preserve-digests``: skopeo
fetches only blobs the layout lacks and checks every blob and the manifest
against the pinned digest. A Spark build's uploaded archive is converted into
the layout by the Controller, which produced its identity. After that, the
layout is read by name and size only; nothing is re-hashed.

Only OCI image manifests are admitted, because the agent site serves every
manifest with the OCI manifest media type.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

IMAGE_CACHE_DIRECTORY = "image-cache"
LAYOUT_DIRECTORY = "oci"
OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
STORE_BUSY = "image_store.busy"
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_COPY_TIMEOUT_SECONDS = 6 * 60 * 60
_COPY_RETRIES = 3
# The layout's own index only names the newest import; GC follows the
# Controller's references, not this index.
_IMPORT_REFERENCE = "import"


class OciImageStoreError(RuntimeError):
    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail[:512]
        super().__init__(self.detail)


@dataclass(frozen=True, slots=True)
class StoredImage:
    """One complete image in the layout.

    ``stored_bytes`` is the size of its layers as stored and transferred
    (compressed when the source compressed them); manifest and config are
    small metadata beside them.
    """

    manifest_digest: str
    config_digest: str
    layer_digests: tuple[str, ...]
    stored_bytes: int

    @property
    def blob_digests(self) -> tuple[str, ...]:
        return (self.manifest_digest, self.config_digest, *self.layer_digests)


Runner = Callable[..., subprocess.CompletedProcess[str]]


class OciImageStore:
    def __init__(
        self,
        artifact_root: Path,
        *,
        skopeo: str = "/usr/bin/skopeo",
        runner: Runner = subprocess.run,
    ) -> None:
        self.root = artifact_root / IMAGE_CACHE_DIRECTORY / LAYOUT_DIRECTORY
        self._skopeo = skopeo
        self._runner = runner

    def blob_path(self, digest: str) -> Path:
        if _DIGEST.fullmatch(digest) is None:
            raise OciImageStoreError("image_store.digest_invalid", "digest is invalid")
        return self.root / "blobs" / "sha256" / digest.removeprefix("sha256:")

    def read(self, manifest_digest: str) -> StoredImage | None:
        """The stored image, or ``None`` when any of its blobs is missing."""

        try:
            payload = self.blob_path(manifest_digest).read_bytes()
            manifest = json.loads(payload)
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as error:
            raise OciImageStoreError(
                "image_store.manifest_unreadable",
                f"stored manifest is unreadable: {error}",
            ) from error
        image = _stored_image(manifest_digest, manifest)
        sizes = {
            image.config_digest: _descriptor_size(manifest["config"]),
            **{
                str(layer["digest"]): _descriptor_size(layer)
                for layer in manifest["layers"]
            },
        }
        for digest, size in sizes.items():
            try:
                if self.blob_path(digest).stat().st_size != size:
                    return None
            except FileNotFoundError:
                return None
        return image

    def import_reference(self, reference: str) -> StoredImage:
        """Copy a digest-pinned registry image into the layout."""

        _, _, digest = reference.rpartition("@")
        if _DIGEST.fullmatch(digest) is None:
            raise OciImageStoreError(
                "image_store.reference_unpinned", "image reference is not digest-pinned"
            )
        self._copy(
            [
                "--preserve-digests",
                "--override-os",
                "linux",
                "--override-arch",
                "arm64",
                f"docker://{reference}",
            ]
        )
        image = self.read(digest)
        if image is None:
            raise OciImageStoreError(
                "image_store.import_incomplete", "copied image is incomplete"
            )
        return image

    def import_archive(self, archive: Path) -> StoredImage:
        """Convert a Docker archive (a Spark build) into the layout."""

        return self._copy([f"docker-archive:{archive}"])

    def _copy(self, source: list[str]) -> StoredImage:
        self.root.mkdir(mode=0o755, parents=True, exist_ok=True)
        digest_file = self.root / ".import.digest"
        with self._lock():
            digest_file.unlink(missing_ok=True)
            command = [
                self._skopeo,
                "copy",
                "--retry-times",
                str(_COPY_RETRIES),
                "--digestfile",
                str(digest_file),
                *source,
                f"oci:{self.root}:{_IMPORT_REFERENCE}",
            ]
            try:
                result = self._runner(
                    command,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=_COPY_TIMEOUT_SECONDS,
                    env=os.environ | {"TMPDIR": str(self.root.parent)},
                )
            except (OSError, subprocess.TimeoutExpired) as error:
                raise OciImageStoreError(
                    "image_store.copy_failed", f"skopeo copy did not finish: {error}"
                ) from error
            if result.returncode != 0:
                detail = (
                    result.stderr or result.stdout or "skopeo copy failed"
                ).strip()
                raise OciImageStoreError(
                    "image_store.copy_failed", detail.splitlines()[-1]
                )
            try:
                manifest_digest = digest_file.read_text(encoding="utf-8").strip()
            except OSError as error:
                raise OciImageStoreError(
                    "image_store.copy_failed", "skopeo reported no manifest digest"
                ) from error
            finally:
                digest_file.unlink(missing_ok=True)
        image = self.read(manifest_digest)
        if image is None:
            raise OciImageStoreError(
                "image_store.import_incomplete", "copied image is incomplete"
            )
        return image

    @contextmanager
    def _lock(self) -> Iterator[None]:
        """One writer at a time: skopeo rewrites the layout's index.

        Never waits: a busy store is reported, and the caller retries later.
        """

        descriptor = os.open(self.root / ".lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise OciImageStoreError(
                    STORE_BUSY, "another image is being stored"
                ) from error
            yield
        finally:
            os.close(descriptor)


def _descriptor_size(descriptor: object) -> int:
    size = descriptor.get("size") if isinstance(descriptor, Mapping) else None
    if type(size) is not int or size < 0:
        raise OciImageStoreError(
            "image_store.manifest_invalid", "manifest descriptor size is invalid"
        )
    return size


def _stored_image(manifest_digest: str, manifest: object) -> StoredImage:
    if (
        not isinstance(manifest, Mapping)
        or manifest.get("mediaType") != OCI_MANIFEST
        or not isinstance(manifest.get("config"), Mapping)
        or not isinstance(manifest.get("layers"), list)
    ):
        raise OciImageStoreError(
            "image_store.manifest_unsupported",
            "only single-platform OCI image manifests are stored",
        )
    config = manifest["config"]
    layers = manifest["layers"]
    digests = [
        config.get("digest"),
        *(
            layer.get("digest") if isinstance(layer, Mapping) else None
            for layer in layers
        ),
    ]
    if not all(
        isinstance(value, str) and _DIGEST.fullmatch(value) for value in digests
    ):
        raise OciImageStoreError(
            "image_store.manifest_invalid", "manifest descriptor digest is invalid"
        )
    return StoredImage(
        manifest_digest=manifest_digest,
        config_digest=str(config["digest"]),
        layer_digests=tuple(str(layer["digest"]) for layer in layers),
        stored_bytes=sum(_descriptor_size(layer) for layer in layers),
    )


__all__ = [
    "IMAGE_CACHE_DIRECTORY",
    "LAYOUT_DIRECTORY",
    "OCI_MANIFEST",
    "STORE_BUSY",
    "OciImageStore",
    "OciImageStoreError",
    "StoredImage",
]
