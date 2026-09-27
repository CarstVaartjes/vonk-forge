from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="GitHubReleaseAsset")



@_attrs_define
class GitHubReleaseAsset:
    """ One GitHub release asset selected for an existing model file.

        Attributes:
            asset_id (int):
            file_id (str):
     """

    asset_id: int
    file_id: str





    def to_dict(self) -> dict[str, Any]:
        asset_id = self.asset_id

        file_id = self.file_id


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "asset_id": asset_id,
            "file_id": file_id,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        asset_id = d.pop("asset_id")

        file_id = d.pop("file_id")

        git_hub_release_asset = cls(
            asset_id=asset_id,
            file_id=file_id,
        )

        return git_hub_release_asset
