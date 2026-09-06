from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import Literal, cast






T = TypeVar("T", bound="RecipeImage")



@_attrs_define
class RecipeImage:
    """
        Attributes:
            digest (str):
            platform (Literal['linux/arm64']):
            repository (str):
     """

    digest: str
    platform: Literal['linux/arm64']
    repository: str





    def to_dict(self) -> dict[str, Any]:
        digest = self.digest

        platform = self.platform

        repository = self.repository


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "digest": digest,
            "platform": platform,
            "repository": repository,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        digest = d.pop("digest")

        platform = cast(Literal['linux/arm64'] , d.pop("platform"))
        if platform != 'linux/arm64':
            raise ValueError(f"platform must match const 'linux/arm64', got '{platform}'")

        repository = d.pop("repository")

        recipe_image = cls(
            digest=digest,
            platform=platform,
            repository=repository,
        )

        return recipe_image
