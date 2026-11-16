"""Distribution: model cache."""

from __future__ import annotations

from collections.abc import Callable
from threading import Lock
from typing import TYPE_CHECKING, cast

from vonk_agent_protocol import (
    DistributionCode,
    DistributionObject,
    SecurityRefusalError,
    SecurityRefusalReason,
)

from ..compiled_execution_plan import DistributionObjectReceipt, VerifiedModelObject
from ..model_cache.errors import ModelCacheNotFound

if TYPE_CHECKING:
    from ..model_cache import ModelCacheService


from .types import (
    DistributionError,
    DistributionRefused,
    DistributionUnknown,
    OpenedObject,
    same_content,
)


def model_file_receipt(
    *,
    model_content_sha256: str,
    file_id: str,
    item: DistributionObject,
    roles: list[str],
) -> VerifiedModelObject:
    """One model file's receipt: its model, file identity and content digest."""

    return VerifiedModelObject(
        model_content_sha256=model_content_sha256,
        file_id=file_id,
        path=item.name,
        sha256=item.sha256,
        bytes=item.bytes,
        roles=roles,
        distribution_object=DistributionObjectReceipt(
            name=item.name, sha256=item.sha256, bytes=item.bytes, kind="model"
        ),
    )


# Process-local memo of recently asked sets; the stored manifest stays the authority.
_MEMOISED_SETS = 64


class ModelCacheObjectSource:
    """Narrow adapter for the NAS model-cache verified-object service.

    ``manifests`` is keyed by the cache service's own artifact-set digest and
    contains the complete ordered model manifest.  This adapter never derives
    or rewrites that digest; the cache worker remains its authority.
    """

    # The source owns verified-object availability; its type also preserves
    # the literal-dispatch edge used by the retry proof.
    _service: ModelCacheService
    _manifests: dict[str, tuple[DistributionObject, ...]]
    _paths: dict[str, tuple[str, str, object]]
    _open_object: Callable[[str, int], OpenedObject]

    def __init__(
        self,
        open_object: Callable[[str, int], OpenedObject],
        manifests: dict[str, tuple[DistributionObject, ...]],
    ) -> None:
        self._open_object = open_object
        self._metadata_guard = Lock()
        self._paths = {}
        self._manifests = dict(manifests)

    def open_object(self, digest: str, expected_bytes: int) -> OpenedObject:
        if hasattr(self, "_service"):
            return self._open_cache_object(digest, expected_bytes)
        return self._open_object(digest, expected_bytes)

    def verify_runtime_image(self, image_digest: str, archive_sha256: str) -> bool:
        return False

    @classmethod
    def from_service(cls, service: object) -> ModelCacheObjectSource:
        """Construct directly from the NAS worker's verified-object service."""
        return cls._from_cache_service(service)

    @classmethod
    def _from_cache_service(cls, service: object) -> ModelCacheObjectSource:
        adapter = cls.__new__(cls)
        adapter._service = cast("ModelCacheService", service)
        adapter._metadata_guard = Lock()
        adapter._manifests = {}
        adapter._paths = {}
        return adapter

    def _load_manifest(self, digest: str) -> tuple[DistributionObject, ...]:
        objects, paths = self._describe(digest)
        # Stage every descriptor before making authorization visible. The guard
        # protects only process-local metadata; cache/SQL/file IO occurs outside.
        with self._metadata_guard:
            self._manifests.pop(digest, None)
            self._paths.update(paths)
            self._manifests[digest] = objects
            # Newest sets stay; the oldest set's authorization is re-derived
            # from the stored manifest the next time it is asked for.
            while len(self._manifests) > _MEMOISED_SETS:
                oldest = next(iter(self._manifests))
                for evicted in self._manifests.pop(oldest):
                    if self._paths.get(evicted.sha256, ("",))[0] == oldest:
                        del self._paths[evicted.sha256]
        return objects

    def _describe(
        self, digest: str
    ) -> tuple[
        tuple[DistributionObject, ...],
        dict[str, tuple[str, str, object]],
    ]:
        try:
            # ModelCacheService validates its opaque digest against the full
            # canonical ArtifactSetManifest before exposing descriptors.
            manifest_provider = getattr(
                self._service, "manifest_for_artifact_set", None
            )
            descriptor_provider = getattr(
                self._service, "resolve_verified_artifact_set", None
            )
            if not isinstance(manifest_provider, Callable) or not isinstance(
                descriptor_provider, Callable
            ):
                raise TypeError("NAS cache manifest provider is unavailable")
            manifest = manifest_provider(digest)
            if getattr(manifest, "digest", None) != digest:
                raise ValueError("cache manifest identity changed")
            descriptors = descriptor_provider(digest)
        except SecurityRefusalError:
            raise
        except ModelCacheNotFound as error:
            # Only a set the cache no longer holds is definite: the agent ends
            # the order and the Controller plans a download. Any other cause (a
            # database or pool error, an unreadable file, incomplete coverage)
            # clears by itself and stays a wait.
            raise DistributionUnknown(
                DistributionCode.MODEL_SET_IDENTITY_UNAVAILABLE,
                "NAS cache manifest is unavailable",
            ) from error
        except Exception as error:
            raise DistributionUnknown(
                DistributionCode.OBJECT_UNAVAILABLE,
                "NAS cache manifest is unavailable",
            ) from error
        objects = []
        paths = {}
        for descriptor in descriptors:
            try:
                item = DistributionObject(
                    name=str(descriptor["path"]),
                    sha256=str(descriptor["sha256"]),
                    bytes=int(descriptor["bytes"]),
                    kind="model",
                )
                path = descriptor["file"]
                objects.append(item)
                paths[item.sha256] = (digest, item.name, path)
            except (KeyError, TypeError, ValueError) as error:
                raise DistributionUnknown(
                    DistributionCode.OBJECT_UNAVAILABLE,
                    "NAS cache manifest is malformed",
                ) from error
        return tuple(objects), paths

    def _open_cache_object(self, digest: str, expected_bytes: int) -> OpenedObject:
        with self._metadata_guard:
            entry = self._paths.get(digest)
        if entry is None:
            raise DistributionUnknown(
                DistributionCode.OBJECT_UNAVAILABLE,
                "NAS cache object was not authorized",
            )
        set_digest, path, _ = entry
        try:
            file_provider = getattr(self._service, "cached_artifact_file", None)
            if not isinstance(file_provider, Callable):
                raise TypeError("NAS cache object provider is unavailable")
            verified_path, size, verified_digest = file_provider(
                set_digest, digest, path
            )
            if size != expected_bytes or verified_digest != digest:
                raise DistributionUnknown(
                    DistributionCode.OBJECT_UNAVAILABLE,
                    "NAS cache object identity changed",
                )
            return OpenedObject(verified_path.open("rb"), size, digest, verified_path)
        except DistributionError:
            raise
        except SecurityRefusalError as error:
            raise DistributionRefused(
                getattr(
                    error,
                    "code",
                    (error.typed_reason or SecurityRefusalReason.FORBIDDEN).value,
                ),
                "NAS cache object access was refused",
                reason=error.typed_reason or SecurityRefusalReason.FORBIDDEN,
            ) from error
        except PermissionError as error:
            raise DistributionRefused(
                SecurityRefusalReason.PERMISSION_DENIED,
                "NAS cache object access was denied",
                reason=SecurityRefusalReason.PERMISSION_DENIED,
            ) from error
        except ModelCacheNotFound as error:
            raise DistributionUnknown(
                DistributionCode.MODEL_SET_IDENTITY_UNAVAILABLE,
                "NAS cache object is no longer held",
            ) from error
        except Exception as error:
            raise DistributionUnknown(
                DistributionCode.OBJECT_UNAVAILABLE, "NAS cache object is unavailable"
            ) from error

    def verify_artifact_set(
        self, artifact_set_sha256: str, objects: tuple[DistributionObject, ...]
    ) -> bool:
        # Content identity: the assignment carries each digest once, so the
        # stored manifest's paths that repeat a digest are not a mismatch.
        if same_content(self.objects_for_set(artifact_set_sha256), objects):
            return True
        if not hasattr(self, "_service"):
            return False
        # The stored manifest decides, never this process's copy of it.
        return same_content(self._load_manifest(artifact_set_sha256), objects)

    def objects_for_set(
        self, artifact_set_sha256: str
    ) -> tuple[DistributionObject, ...]:
        """Return the verified complete model manifest for assignment creation."""
        with self._metadata_guard:
            cached = self._manifests.get(artifact_set_sha256)
        return (
            cached if cached is not None else self._load_manifest(artifact_set_sha256)
        )
