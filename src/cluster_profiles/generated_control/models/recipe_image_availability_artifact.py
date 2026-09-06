from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Union






T = TypeVar("T", bound="RecipeImageAvailabilityArtifact")



@_attrs_define
class RecipeImageAvailabilityArtifact:
    """
        Attributes:
            download_bytes (int):
            id (str):
            key (str):
            kind (str):
            path (str):
            roles (list[str]):
            sha256 (str):
            source (str):
            model_version_sha256 (Union[None, Unset, str]):
            repository (Union[None, Unset, str]):
            revision (Union[None, Unset, str]):
     """

    download_bytes: int
    id: str
    key: str
    kind: str
    path: str
    roles: list[str]
    sha256: str
    source: str
    model_version_sha256: Union[None, Unset, str] = UNSET
    repository: Union[None, Unset, str] = UNSET
    revision: Union[None, Unset, str] = UNSET





    def to_dict(self) -> dict[str, Any]:
        download_bytes = self.download_bytes

        id = self.id

        key = self.key

        kind = self.kind

        path = self.path

        roles = self.roles



        sha256 = self.sha256

        source = self.source

        model_version_sha256: Union[None, Unset, str]
        if isinstance(self.model_version_sha256, Unset):
            model_version_sha256 = UNSET
        else:
            model_version_sha256 = self.model_version_sha256

        repository: Union[None, Unset, str]
        if isinstance(self.repository, Unset):
            repository = UNSET
        else:
            repository = self.repository

        revision: Union[None, Unset, str]
        if isinstance(self.revision, Unset):
            revision = UNSET
        else:
            revision = self.revision


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "download_bytes": download_bytes,
            "id": id,
            "key": key,
            "kind": kind,
            "path": path,
            "roles": roles,
            "sha256": sha256,
            "source": source,
        })
        if model_version_sha256 is not UNSET:
            field_dict["model_version_sha256"] = model_version_sha256
        if repository is not UNSET:
            field_dict["repository"] = repository
        if revision is not UNSET:
            field_dict["revision"] = revision

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        download_bytes = d.pop("download_bytes")

        id = d.pop("id")

        key = d.pop("key")

        kind = d.pop("kind")

        path = d.pop("path")

        roles = cast(list[str], d.pop("roles"))


        sha256 = d.pop("sha256")

        source = d.pop("source")

        def _parse_model_version_sha256(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        model_version_sha256 = _parse_model_version_sha256(d.pop("model_version_sha256", UNSET))


        def _parse_repository(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        repository = _parse_repository(d.pop("repository", UNSET))


        def _parse_revision(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        revision = _parse_revision(d.pop("revision", UNSET))


        recipe_image_availability_artifact = cls(
            download_bytes=download_bytes,
            id=id,
            key=key,
            kind=kind,
            path=path,
            roles=roles,
            sha256=sha256,
            source=source,
            model_version_sha256=model_version_sha256,
            repository=repository,
            revision=revision,
        )

        return recipe_image_availability_artifact
