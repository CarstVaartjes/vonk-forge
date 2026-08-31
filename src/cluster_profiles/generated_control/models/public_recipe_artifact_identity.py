from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast






T = TypeVar("T", bound="PublicRecipeArtifactIdentity")



@_attrs_define
class PublicRecipeArtifactIdentity:
    """
        Attributes:
            artifact_id (str):
            download_bytes (int):
            identity_sha256 (str):
            installed_bytes (int):
            roles (list[str]):
     """

    artifact_id: str
    download_bytes: int
    identity_sha256: str
    installed_bytes: int
    roles: list[str]





    def to_dict(self) -> dict[str, Any]:
        artifact_id = self.artifact_id

        download_bytes = self.download_bytes

        identity_sha256 = self.identity_sha256

        installed_bytes = self.installed_bytes

        roles = self.roles




        field_dict: dict[str, Any] = {}

        field_dict.update({
            "artifact_id": artifact_id,
            "download_bytes": download_bytes,
            "identity_sha256": identity_sha256,
            "installed_bytes": installed_bytes,
            "roles": roles,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        artifact_id = d.pop("artifact_id")

        download_bytes = d.pop("download_bytes")

        identity_sha256 = d.pop("identity_sha256")

        installed_bytes = d.pop("installed_bytes")

        roles = cast(list[str], d.pop("roles"))


        public_recipe_artifact_identity = cls(
            artifact_id=artifact_id,
            download_bytes=download_bytes,
            identity_sha256=identity_sha256,
            installed_bytes=installed_bytes,
            roles=roles,
        )

        return public_recipe_artifact_identity
