"""Distribution: sources."""

from __future__ import annotations

import hashlib
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory

from vonk_agent_protocol import (
    DistributionCode,
    DistributionObject,
    SecurityRefusalError,
)

from ..bounded_retry import bounded_attempts
from ..compiled_execution_plan import VerifiedModelObject
from .model_cache import ModelCacheObjectSource
from .types import (
    DistributionError,
    DistributionUnknown,
    ObjectSource,
    OpenedObject,
)


def verified_model_receipts(
    service: object, artifact_set_sha256: str, manifest: object
) -> tuple[VerifiedModelObject, ...]:
    """Resolve canonical file receipts through the typed NAS adapter boundary."""

    source: ModelCacheObjectSource = ModelCacheObjectSource.from_service(service)
    return source.verified_model_objects_for_set(artifact_set_sha256, manifest)


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

    def verified_model_objects_for_set(
        self, artifact_set_sha256: str, manifest: object = None
    ) -> tuple[VerifiedModelObject, ...]:
        """Re-observe a repaired service binding within the request budget."""
        missing: DistributionUnknown | None = None
        for _attempt in bounded_attempts():
            try:
                return self._verified_model_objects_for_set_once(
                    artifact_set_sha256, manifest
                )
            except DistributionUnknown as error:
                missing = error
        assert missing is not None
        raise missing

    def _verified_model_objects_for_set_once(
        self, artifact_set_sha256: str, manifest: object = None
    ) -> tuple[VerifiedModelObject, ...]:
        resolver = getattr(self.model_source, "verified_model_objects_for_set", None)
        if resolver is None:
            raise DistributionUnknown(
                DistributionCode.MODEL_SET_IDENTITY_UNAVAILABLE,
                "NAS cache source lacks canonical model-file identity",
            )
        receipts = (
            resolver(artifact_set_sha256)
            if manifest is None
            else resolver(artifact_set_sha256, manifest)
        )
        return tuple(VerifiedModelObject.model_validate(item) for item in receipts)

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
        expected = tuple(item for item in objects if item.kind == "model")
        # Fixtures receive the opaque digest from the cache manifest. The
        # production NAS adapter above performs the same exact lookup.
        return self.artifact_manifests.get(artifact_set_sha256) == expected

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
