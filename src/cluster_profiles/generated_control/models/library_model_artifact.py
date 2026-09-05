from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.library_model_artifact_kind import check_library_model_artifact_kind
from ..models.library_model_artifact_kind import LibraryModelArtifactKind
from typing import cast






T = TypeVar("T", bound="LibraryModelArtifact")



@_attrs_define
class LibraryModelArtifact:
    """
        Attributes:
            download_bytes (int):
            id (str):
            installed_bytes (int):
            kind (LibraryModelArtifactKind):
            path (str):
            repository (str):
            revision (str):
            roles (list[str]):
            sha256 (str):
     """

    download_bytes: int
    id: str
    installed_bytes: int
    kind: LibraryModelArtifactKind
    path: str
    repository: str
    revision: str
    roles: list[str]
    sha256: str





    def to_dict(self) -> dict[str, Any]:
        download_bytes = self.download_bytes

        id = self.id

        installed_bytes = self.installed_bytes

        kind: str = self.kind

        path = self.path

        repository = self.repository

        revision = self.revision

        roles = self.roles



        sha256 = self.sha256


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "download_bytes": download_bytes,
            "id": id,
            "installed_bytes": installed_bytes,
            "kind": kind,
            "path": path,
            "repository": repository,
            "revision": revision,
            "roles": roles,
            "sha256": sha256,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        download_bytes = d.pop("download_bytes")

        id = d.pop("id")

        installed_bytes = d.pop("installed_bytes")

        kind = check_library_model_artifact_kind(d.pop("kind"))




        path = d.pop("path")

        repository = d.pop("repository")

        revision = d.pop("revision")

        roles = cast(list[str], d.pop("roles"))


        sha256 = d.pop("sha256")

        library_model_artifact = cls(
            download_bytes=download_bytes,
            id=id,
            installed_bytes=installed_bytes,
            kind=kind,
            path=path,
            repository=repository,
            revision=revision,
            roles=roles,
            sha256=sha256,
        )

        return library_model_artifact
