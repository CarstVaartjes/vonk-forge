from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.operational_build_state import check_operational_build_state
from ..models.operational_build_state import OperationalBuildState
from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Union






T = TypeVar("T", bound="OperationalBuild")



@_attrs_define
class OperationalBuild:
    """
        Attributes:
            image_digest (Union[None, str]):
            recipe_build_id (str):
            recipe_revision_id (str):
            state (OperationalBuildState):
            image_bytes (Union[None, Unset, int]):
     """

    image_digest: Union[None, str]
    recipe_build_id: str
    recipe_revision_id: str
    state: OperationalBuildState
    image_bytes: Union[None, Unset, int] = UNSET





    def to_dict(self) -> dict[str, Any]:
        image_digest: Union[None, str]
        image_digest = self.image_digest

        recipe_build_id = self.recipe_build_id

        recipe_revision_id = self.recipe_revision_id

        state: str = self.state

        image_bytes: Union[None, Unset, int]
        if isinstance(self.image_bytes, Unset):
            image_bytes = UNSET
        else:
            image_bytes = self.image_bytes


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "image_digest": image_digest,
            "recipe_build_id": recipe_build_id,
            "recipe_revision_id": recipe_revision_id,
            "state": state,
        })
        if image_bytes is not UNSET:
            field_dict["image_bytes"] = image_bytes

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        def _parse_image_digest(data: object) -> Union[None, str]:
            if data is None:
                return data
            return cast(Union[None, str], data)

        image_digest = _parse_image_digest(d.pop("image_digest"))


        recipe_build_id = d.pop("recipe_build_id")

        recipe_revision_id = d.pop("recipe_revision_id")

        state = check_operational_build_state(d.pop("state"))




        def _parse_image_bytes(data: object) -> Union[None, Unset, int]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, int], data)

        image_bytes = _parse_image_bytes(d.pop("image_bytes", UNSET))


        operational_build = cls(
            image_digest=image_digest,
            recipe_build_id=recipe_build_id,
            recipe_revision_id=recipe_revision_id,
            state=state,
            image_bytes=image_bytes,
        )

        return operational_build
