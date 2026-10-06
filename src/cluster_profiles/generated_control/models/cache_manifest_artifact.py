from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast

if TYPE_CHECKING:
  from ..models.cache_manifest_artifact_part import CacheManifestArtifactPart





T = TypeVar("T", bound="CacheManifestArtifact")



@_attrs_define
class CacheManifestArtifact:
    """
        Attributes:
            download_bytes (int):
            id (str):
            key (str):
            kind (str):
            model_content_sha256 (None | str):
            path (str):
            repository (None | str):
            revision (None | str):
            roles (list[str]):
            sha256 (str):
            source (str):
            parts (list[CacheManifestArtifactPart] | None | Unset):
     """

    download_bytes: int
    id: str
    key: str
    kind: str
    model_content_sha256: None | str
    path: str
    repository: None | str
    revision: None | str
    roles: list[str]
    sha256: str
    source: str
    parts: list[CacheManifestArtifactPart] | None | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.cache_manifest_artifact_part import CacheManifestArtifactPart # noqa: PLC0415
        download_bytes = self.download_bytes

        id = self.id

        key = self.key

        kind = self.kind

        model_content_sha256: None | str
        model_content_sha256 = self.model_content_sha256

        path = self.path

        repository: None | str
        repository = self.repository

        revision: None | str
        revision = self.revision

        roles = self.roles



        sha256 = self.sha256

        source = self.source

        parts: list[dict[str, Any]] | None | Unset
        if isinstance(self.parts, Unset):
            parts = UNSET
        elif isinstance(self.parts, list):
            parts = []
            for parts_type_0_item_data in self.parts:
                parts_type_0_item = parts_type_0_item_data.to_dict()
                parts.append(parts_type_0_item)


        else:
            parts = self.parts


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "download_bytes": download_bytes,
            "id": id,
            "key": key,
            "kind": kind,
            "model_content_sha256": model_content_sha256,
            "path": path,
            "repository": repository,
            "revision": revision,
            "roles": roles,
            "sha256": sha256,
            "source": source,
        })
        if parts is not UNSET:
            field_dict["parts"] = parts

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.cache_manifest_artifact_part import CacheManifestArtifactPart # noqa: PLC0415
        d = dict(src_dict)
        download_bytes = d.pop("download_bytes")

        id = d.pop("id")

        key = d.pop("key")

        kind = d.pop("kind")

        def _parse_model_content_sha256(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        model_content_sha256 = _parse_model_content_sha256(d.pop("model_content_sha256"))


        path = d.pop("path")

        def _parse_repository(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        repository = _parse_repository(d.pop("repository"))


        def _parse_revision(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        revision = _parse_revision(d.pop("revision"))


        roles = cast(list[str], d.pop("roles"))


        sha256 = d.pop("sha256")

        source = d.pop("source")

        def _parse_parts(data: object) -> list[CacheManifestArtifactPart] | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, list):
                    raise TypeError()
                parts_type_0 = []
                _parts_type_0 = data
                for parts_type_0_item_data in (_parts_type_0):
                    parts_type_0_item = CacheManifestArtifactPart.from_dict(parts_type_0_item_data)



                    parts_type_0.append(parts_type_0_item)

                return parts_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(list[CacheManifestArtifactPart] | None | Unset, data)

        parts = _parse_parts(d.pop("parts", UNSET))


        cache_manifest_artifact = cls(
            download_bytes=download_bytes,
            id=id,
            key=key,
            kind=kind,
            model_content_sha256=model_content_sha256,
            path=path,
            repository=repository,
            revision=revision,
            roles=roles,
            sha256=sha256,
            source=source,
            parts=parts,
        )

        return cache_manifest_artifact
