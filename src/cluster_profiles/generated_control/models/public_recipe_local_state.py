from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.public_recipe_local_state_status import check_public_recipe_local_state_status
from ..models.public_recipe_local_state_status import PublicRecipeLocalStateStatus
from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Union






T = TypeVar("T", bound="PublicRecipeLocalState")



@_attrs_define
class PublicRecipeLocalState:
    """
        Attributes:
            status (PublicRecipeLocalStateStatus):
            content_sha256 (Union[None, Unset, str]):
            recipe_id (Union[None, Unset, str]):
            release_version (Union[None, Unset, str]):
            revision_number (Union[None, Unset, int]):
     """

    status: PublicRecipeLocalStateStatus
    content_sha256: Union[None, Unset, str] = UNSET
    recipe_id: Union[None, Unset, str] = UNSET
    release_version: Union[None, Unset, str] = UNSET
    revision_number: Union[None, Unset, int] = UNSET





    def to_dict(self) -> dict[str, Any]:
        status: str = self.status

        content_sha256: Union[None, Unset, str]
        if isinstance(self.content_sha256, Unset):
            content_sha256 = UNSET
        else:
            content_sha256 = self.content_sha256

        recipe_id: Union[None, Unset, str]
        if isinstance(self.recipe_id, Unset):
            recipe_id = UNSET
        else:
            recipe_id = self.recipe_id

        release_version: Union[None, Unset, str]
        if isinstance(self.release_version, Unset):
            release_version = UNSET
        else:
            release_version = self.release_version

        revision_number: Union[None, Unset, int]
        if isinstance(self.revision_number, Unset):
            revision_number = UNSET
        else:
            revision_number = self.revision_number


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "status": status,
        })
        if content_sha256 is not UNSET:
            field_dict["content_sha256"] = content_sha256
        if recipe_id is not UNSET:
            field_dict["recipe_id"] = recipe_id
        if release_version is not UNSET:
            field_dict["release_version"] = release_version
        if revision_number is not UNSET:
            field_dict["revision_number"] = revision_number

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        status = check_public_recipe_local_state_status(d.pop("status"))




        def _parse_content_sha256(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        content_sha256 = _parse_content_sha256(d.pop("content_sha256", UNSET))


        def _parse_recipe_id(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        recipe_id = _parse_recipe_id(d.pop("recipe_id", UNSET))


        def _parse_release_version(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        release_version = _parse_release_version(d.pop("release_version", UNSET))


        def _parse_revision_number(data: object) -> Union[None, Unset, int]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, int], data)

        revision_number = _parse_revision_number(d.pop("revision_number", UNSET))


        public_recipe_local_state = cls(
            status=status,
            content_sha256=content_sha256,
            recipe_id=recipe_id,
            release_version=release_version,
            revision_number=revision_number,
        )

        return public_recipe_local_state
