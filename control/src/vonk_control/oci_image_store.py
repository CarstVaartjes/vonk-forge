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

Collection removes what no image the Controller still names refers to. A copy
marks every blob of the image it stored as fresh, so a just-stored image is
kept for the grace period whatever the database says yet; after that, only
the named images keep their blobs. Collection and copies share the store's
one writer lock, so they never interleave.

A collection deletes only on positive evidence. If any referenced image's
manifest cannot be read or understood (an I/O or permission error, truncated
JSON, an unknown schema), the blobs that image refers to are unknown, so the
whole pass deletes nothing, names the reason, and runs again next round.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from vonk_agent_protocol import ImageStoreCode

IMAGE_CACHE_DIRECTORY = "image-cache"
LAYOUT_DIRECTORY = "oci"
OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
STORE_BUSY = ImageStoreCode.BUSY
REFERENCE_SCAN_FAILED = ImageStoreCode.REFERENCE_SCAN_FAILED
#: A manifest a receipt names exists but its content is damaged (not a
#: permission or I/O fault): ``OciImageStoreError.address`` names it.
REFERENCED_MANIFEST_DAMAGED = ImageStoreCode.REFERENCED_MANIFEST_DAMAGED
#: Codes of a stored manifest whose content is damaged or not an image this
#: store holds. Reading it as absent is cache loss; an OS error is not.
DAMAGED_MANIFEST_CODES = frozenset(
    {
        ImageStoreCode.MANIFEST_CORRUPT,
        ImageStoreCode.MANIFEST_INVALID,
        ImageStoreCode.MANIFEST_UNSUPPORTED,
    }
)
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_COPY_TIMEOUT_SECONDS = 6 * 60 * 60
_COPY_RETRIES = 3
# The layout's own index only names the newest import; GC follows the
# Controller's references, not this index.
_IMPORT_REFERENCE = "import"


class OciImageStoreError(RuntimeError):
    def __init__(self, code: str, detail: str, *, address: str | None = None) -> None:
        self.code = code
        self.detail = detail[:512]
        self.address = address
        super().__init__(self.detail)


@dataclass(frozen=True, slots=True)
class StoreUnknown:
    """The store could not settle the answer (busy, unreadable, copy unfinished).

    Not a refusal and not an error to unwind: nothing was removed and nothing
    half-stored is trusted, so the owner observes again on its next pass.
    ``address`` names the manifest involved when one is.
    """

    code: str
    detail: str
    address: str | None = None


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


@dataclass(frozen=True, slots=True)
class Collection:
    """What one collection removed from the layout."""

    blobs_removed: int
    bytes_reclaimed: int


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
            raise OciImageStoreError(ImageStoreCode.DIGEST_INVALID, "digest is invalid")
        return self.root / "blobs" / "sha256" / digest.removeprefix("sha256:")

    def read(self, manifest_digest: str) -> StoredImage | StoreUnknown | None:
        """The stored image, ``None`` when any of its blobs is missing, or
        :class:`StoreUnknown` when the manifest cannot be read right now."""

        try:
            payload = self.blob_path(manifest_digest).read_bytes()
            manifest = json.loads(payload)
        except FileNotFoundError:
            return None
        except ValueError:
            return None
        except OSError as error:
            return StoreUnknown(
                ImageStoreCode.MANIFEST_UNREADABLE,
                f"stored manifest is unreadable: {error}"[:512],
            )
        try:
            image = _stored_image(manifest_digest, manifest)
            sizes = {
                image.config_digest: _descriptor_size(manifest["config"]),
                **{
                    str(layer["digest"]): _descriptor_size(layer)
                    for layer in manifest["layers"]
                },
            }
        except OciImageStoreError:
            # These are stored descriptors, not submitted ingress evidence.
            # Their damage makes this image a miss for normal exact reprepare.
            return None
        for digest, size in sizes.items():
            try:
                if self.blob_path(digest).stat().st_size != size:
                    return None
            except FileNotFoundError:
                return None
        return image

    def import_reference(self, reference: str) -> StoredImage | StoreUnknown:
        """Copy a digest-pinned registry image into the layout.

        :class:`StoreUnknown` when the copy could not be settled; a reference
        that is not digest-pinned is refused as a malformed request.
        """

        _, _, digest = reference.rpartition("@")
        if _DIGEST.fullmatch(digest) is None:
            raise OciImageStoreError(
                ImageStoreCode.REFERENCE_UNPINNED,
                "image reference is not digest-pinned",
            )
        copied = self._copy(
            [
                "--preserve-digests",
                "--override-os",
                "linux",
                "--override-arch",
                "arm64",
                f"docker://{reference}",
            ]
        )
        if isinstance(copied, StoreUnknown):
            return copied
        image = self.read(digest)
        if isinstance(image, StoreUnknown):
            return image
        if image is None:
            return StoreUnknown(
                ImageStoreCode.IMPORT_INCOMPLETE, "copied image is incomplete"
            )
        return image

    def import_archive(self, archive: Path) -> StoredImage | StoreUnknown:
        """Convert a Docker archive (a Spark build) into the layout."""

        return self._copy([f"docker-archive:{archive}"])

    def _copy(self, source: list[str]) -> StoredImage | StoreUnknown:
        self.root.mkdir(mode=0o755, parents=True, exist_ok=True)
        digest_file = self.root / ".import.digest"
        with self._lock() as held:
            if not held:
                return StoreUnknown(STORE_BUSY, "another image is being stored")
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
                return StoreUnknown(
                    ImageStoreCode.COPY_FAILED,
                    f"skopeo copy did not finish: {error}"[:512],
                )
            if result.returncode != 0:
                detail = (
                    result.stderr or result.stdout or "skopeo copy failed"
                ).strip()
                return StoreUnknown(
                    ImageStoreCode.COPY_FAILED, detail.splitlines()[-1][:512]
                )
            try:
                manifest_digest = digest_file.read_text(encoding="utf-8").strip()
            except OSError:
                return StoreUnknown(
                    ImageStoreCode.COPY_FAILED, "skopeo reported no manifest digest"
                )
            finally:
                digest_file.unlink(missing_ok=True)
            image = self.read(manifest_digest)
            if isinstance(image, StoreUnknown):
                return image
            if image is None:
                return StoreUnknown(
                    ImageStoreCode.IMPORT_INCOMPLETE, "copied image is incomplete"
                )
            # A copy that found every blob already present writes nothing;
            # mark the image fresh so collection keeps it for the grace
            # period, until the Controller has recorded it.
            for digest in image.blob_digests:
                os.utime(self.blob_path(digest))
        return image

    def collect(
        self,
        referenced: Callable[[], Iterable[str] | StoreUnknown],
        *,
        grace_seconds: float,
    ) -> Collection | StoreUnknown:
        """Remove blobs no referenced image needs.

        ``referenced`` returns the manifests (hex addresses) the Controller
        still needs; it is asked while the writer lock is held, so no copy
        can land between the answer and the sweep. A blob is removed only
        when none of them refers to it and it is older than the grace
        period. A missing referenced manifest refers to nothing, which is
        ordinary cache loss. A manifest that exists but cannot be read or
        understood proves nothing, and neither does a reference list that could
        not be read: the pass answers :class:`StoreUnknown` before anything is
        removed (``REFERENCE_SCAN_FAILED`` / ``REFERENCED_MANIFEST_DAMAGED``),
        because a failed scan never proves a blob unused. A busy store answers
        ``STORE_BUSY`` the same way.
        """

        blobs = self.root / "blobs" / "sha256"
        if not blobs.is_dir():
            return Collection(0, 0)
        with self._lock() as held:
            if not held:
                return StoreUnknown(STORE_BUSY, "another image is being stored")
            return self._sweep(blobs, referenced, time.time() - grace_seconds)

    def _sweep(
        self,
        blobs: Path,
        referenced: Callable[[], Iterable[str] | StoreUnknown],
        cutoff: float,
    ) -> Collection | StoreUnknown:
        named = referenced()
        if isinstance(named, StoreUnknown):
            return named
        keep: set[str] = set()
        for address in named:
            kept = self._referenced_blobs(address)
            if isinstance(kept, StoreUnknown):
                return kept
            keep.update(kept)
        removed = reclaimed = 0
        for blob in blobs.iterdir():
            if blob.name in keep:
                continue
            try:
                status = blob.stat()
                if status.st_mtime > cutoff:
                    continue
                blob.unlink()
            except FileNotFoundError:
                continue
            removed += 1
            reclaimed += status.st_size
        return Collection(removed, reclaimed)

    def _referenced_blobs(self, address: str) -> set[str] | StoreUnknown:
        digest = f"sha256:{address}"
        if _DIGEST.fullmatch(digest) is None:
            # No stored manifest can have this name: a definite answer.
            return set()
        try:
            manifest = json.loads(self.blob_path(digest).read_bytes())
            image = _stored_image(digest, manifest)
        except FileNotFoundError:
            return set()
        except (ValueError, OciImageStoreError, OSError) as error:
            damaged = (
                isinstance(error, ValueError)
                or isinstance(error, OciImageStoreError)
                and error.code in DAMAGED_MANIFEST_CODES
            )
            return StoreUnknown(
                REFERENCED_MANIFEST_DAMAGED if damaged else REFERENCE_SCAN_FAILED,
                f"manifest {address} is referenced but cannot be read, "
                f"so nothing was removed: {type(error).__name__}: {error}"[:512],
                address=address,
            )
        return {item.removeprefix("sha256:") for item in image.blob_digests}

    @contextmanager
    def _lock(self) -> Iterator[bool]:
        """One writer at a time: skopeo rewrites the layout's index.

        Never waits: it yields ``False`` for a busy store, and the caller
        answers :class:`StoreUnknown` so its owner retries later.
        """

        descriptor = os.open(self.root / ".lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
            yield True
        finally:
            os.close(descriptor)


def _descriptor_size(descriptor: object) -> int:
    size = descriptor.get("size") if isinstance(descriptor, Mapping) else None
    if type(size) is not int or size < 0:
        raise OciImageStoreError(
            ImageStoreCode.MANIFEST_INVALID, "manifest descriptor size is invalid"
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
            ImageStoreCode.MANIFEST_UNSUPPORTED,
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
            ImageStoreCode.MANIFEST_INVALID, "manifest descriptor digest is invalid"
        )
    return StoredImage(
        manifest_digest=manifest_digest,
        config_digest=str(config["digest"]),
        layer_digests=tuple(str(layer["digest"]) for layer in layers),
        stored_bytes=sum(_descriptor_size(layer) for layer in layers),
    )


__all__ = [
    "DAMAGED_MANIFEST_CODES",
    "IMAGE_CACHE_DIRECTORY",
    "LAYOUT_DIRECTORY",
    "OCI_MANIFEST",
    "REFERENCED_MANIFEST_DAMAGED",
    "REFERENCE_SCAN_FAILED",
    "STORE_BUSY",
    "Collection",
    "OciImageStore",
    "OciImageStoreError",
    "StoreUnknown",
    "StoredImage",
]
