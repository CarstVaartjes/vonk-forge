from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast






T = TypeVar("T", bound="VisualArtifact")



@_attrs_define
class VisualArtifact:
    """
        Attributes:
            download_bytes (int):
            id (str):
            include_paths (list[str]):
            installed_bytes (int):
            kind (str):
            repository (str):
            revision (str):
            roles (list[str]):
     """

    download_bytes: int
    id: str
    include_paths: list[str]
    installed_bytes: int
    kind: str
    repository: str
    revision: str
    roles: list[str]





    def to_dict(self) -> dict[str, Any]:
        download_bytes = self.download_bytes

        id = self.id

        include_paths = self.include_paths



        installed_bytes = self.installed_bytes

        kind = self.kind

        repository = self.repository

        revision = self.revision

        roles = self.roles




        field_dict: dict[str, Any] = {}

        field_dict.update({
            "download_bytes": download_bytes,
            "id": id,
            "include_paths": include_paths,
            "installed_bytes": installed_bytes,
            "kind": kind,
            "repository": repository,
            "revision": revision,
            "roles": roles,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        download_bytes = d.pop("download_bytes")

        id = d.pop("id")

        include_paths = cast(list[str], d.pop("include_paths"))


        installed_bytes = d.pop("installed_bytes")

        kind = d.pop("kind")

        repository = d.pop("repository")

        revision = d.pop("revision")

        roles = cast(list[str], d.pop("roles"))


        visual_artifact = cls(
            download_bytes=download_bytes,
            id=id,
            include_paths=include_paths,
            installed_bytes=installed_bytes,
            kind=kind,
            repository=repository,
            revision=revision,
            roles=roles,
        )

        return visual_artifact
