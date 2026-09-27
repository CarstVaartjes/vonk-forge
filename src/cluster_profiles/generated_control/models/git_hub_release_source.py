from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast
from typing import Literal, cast

if TYPE_CHECKING:
  from ..models.git_hub_release_asset import GitHubReleaseAsset





T = TypeVar("T", bound="GitHubReleaseSource")



@_attrs_define
class GitHubReleaseSource:
    """ An exact asset from one release in a canonical GitHub repository.

        Attributes:
            assets (list['GitHubReleaseAsset']):
            provider (Literal['github-release']):
            release_id (int):
            repository (str):
     """

    assets: list['GitHubReleaseAsset']
    provider: Literal['github-release']
    release_id: int
    repository: str





    def to_dict(self) -> dict[str, Any]:
        from ..models.git_hub_release_asset import GitHubReleaseAsset
        assets = []
        for assets_item_data in self.assets:
            assets_item = assets_item_data.to_dict()
            assets.append(assets_item)



        provider = self.provider

        release_id = self.release_id

        repository = self.repository


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "assets": assets,
            "provider": provider,
            "release_id": release_id,
            "repository": repository,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.git_hub_release_asset import GitHubReleaseAsset
        d = dict(src_dict)
        assets = []
        _assets = d.pop("assets")
        for assets_item_data in (_assets):
            assets_item = GitHubReleaseAsset.from_dict(assets_item_data)



            assets.append(assets_item)


        provider = cast(Literal['github-release'] , d.pop("provider"))
        if provider != 'github-release':
            raise ValueError(f"provider must match const 'github-release', got '{provider}'")

        release_id = d.pop("release_id")

        repository = d.pop("repository")

        git_hub_release_source = cls(
            assets=assets,
            provider=provider,
            release_id=release_id,
            repository=repository,
        )

        return git_hub_release_source
