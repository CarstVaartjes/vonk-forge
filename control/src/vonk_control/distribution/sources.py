"""Distribution: sources."""

from __future__ import annotations

import hashlib
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, cast

from vonk_agent_protocol import (
    DistributionCode,
    DistributionObject,
    SecurityRefusalError,
)

from ..compiled_execution_plan import VerifiedModelObject
from .model_cache import model_file_receipt
from .types import (
    DistributionError,
    DistributionUnknown,
    ObjectSource,
    OpenedObject,
    contained_content,
)

if TYPE_CHECKING:
    from ..model_cache import ModelCacheService
    from ..model_cache.artifacts import ArtifactSetManifest


def verified_model_receipts(
    service: object, artifact_set_sha256: str, manifest: ArtifactSetManifest
) -> tuple[VerifiedModelObject, ...]:
    """Describe a resolved model set for compilation, without its NAS copy.

    A receipt names a model's files by content digest, and the resolved
    manifest is that identity. The NAS copy is read only when target-copy
    sends files to a Spark that lacks them, and that read checks NAS coverage
    itself; so a set every target Spark holds compiles after NAS eviction.
    """

    if manifest.digest != artifact_set_sha256:
        raise DistributionUnknown(
            DistributionCode.MODEL_SET_IDENTITY_UNAVAILABLE,
            "resolved manifest names another artifact set",
        )
    receipts = []
    for spec in manifest.artifacts:
        if spec.model_content_sha256 is None:
            raise DistributionUnknown(
                DistributionCode.MODEL_SET_IDENTITY_UNAVAILABLE,
                "resolved manifest lacks canonical model-file identity",
            )
        receipts.append(
            model_file_receipt(
                model_content_sha256=spec.model_content_sha256,
                file_id=spec.artifact_id,
                item=DistributionObject(
                    name=spec.path,
                    sha256=spec.sha256,
                    bytes=spec.expected_bytes,
                    kind="model",
                ),
                roles=list(spec.roles),
            )
        )
    cast("ModelCacheService", service).adopt_covered_set(manifest)
    return tuple(receipts)


class CompositeObjectSource:
    """Join the NAS model cache and Controller OCI archive boundaries."""

    def __init__(
        self,
        model_source: ObjectSource,
        oci_source: ObjectSource,
    ) -> None:
        self.model_source = model_source
        self.oci_source = oci_source

    def verify_artifact_set(
        self, artifact_set_sha256: str, objects: tuple[DistributionObject, ...]
    ) -> bool:
        return self.model_source.verify_artifact_set(artifact_set_sha256, objects)

    def verify_runtime_image(self, image_digest: str, archive_sha256: str) -> bool:
        return self.oci_source.verify_runtime_image(image_digest, archive_sha256)

    def open_object(self, digest: str, expected_bytes: int) -> OpenedObject:
        # Both sources are content addressed. Probe the model cache first so a
        # shared NAS object is never copied into a second Controller store.
        try:
            return self.model_source.open_object(digest, expected_bytes)
        except SecurityRefusalError:
            raise
        except DistributionError as model_error:
            try:
                return self.oci_source.open_object(digest, expected_bytes)
            except SecurityRefusalError:
                raise
            except DistributionError:
                raise model_error


class MemoryObjectSource:
    """Small deterministic fixture source used by Controller integration tests."""

    def __init__(self, objects: dict[str, bytes] | None = None) -> None:
        # Objects also live as files, because the edge serves stored bytes by path.
        self._directory = TemporaryDirectory(prefix="vonk-objects-")
        self.root = Path(self._directory.name)
        self.objects = dict(objects or {})
        for digest, payload in self.objects.items():
            (self.root / digest).write_bytes(payload)
        self.artifact_manifests: dict[str, tuple[DistributionObject, ...]] = {}
        self.runtime_images: dict[str, str] = {}

    def put(self, payload: bytes) -> str:
        digest = hashlib.sha256(payload).hexdigest()
        self.objects[digest] = bytes(payload)
        (self.root / digest).write_bytes(payload)
        return digest

    def register_runtime_image(self, image_digest: str, archive_sha256: str) -> None:
        self.runtime_images[archive_sha256] = image_digest

    def verify_runtime_image(self, image_digest: str, archive_sha256: str) -> bool:
        return self.runtime_images.get(archive_sha256) == image_digest

    def register_artifact_set(
        self, artifact_set_sha256: str, objects: tuple[DistributionObject, ...]
    ) -> None:
        digest = artifact_set_sha256
        self.artifact_manifests[digest] = tuple(
            item for item in objects if item.kind == "model"
        )

    def verify_artifact_set(
        self, artifact_set_sha256: str, objects: tuple[DistributionObject, ...]
    ) -> bool:
        # Fixtures receive the opaque digest from the cache manifest. The
        # production NAS adapter performs the same content comparison.
        declared = self.artifact_manifests.get(artifact_set_sha256)
        return declared is not None and contained_content(declared, objects)

    def open_object(self, digest: str, expected_bytes: int) -> OpenedObject:
        payload = self.objects.get(digest)
        if payload is None:
            raise DistributionUnknown(
                DistributionCode.OBJECT_UNAVAILABLE, "stored object is unavailable"
            )
        if (
            len(payload) != expected_bytes
            or hashlib.sha256(payload).hexdigest() != digest
        ):
            raise DistributionUnknown(
                DistributionCode.OBJECT_UNAVAILABLE, "verified object digest mismatch"
            )
        return OpenedObject(BytesIO(payload), len(payload), digest, self.root / digest)
