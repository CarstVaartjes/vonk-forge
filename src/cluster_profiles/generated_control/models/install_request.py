from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast, Union
from typing import Union






T = TypeVar("T", bound="InstallRequest")



@_attrs_define
class InstallRequest:
    """
        Attributes:
            mapping_id (str):
            plan_digest (str):
            request_key (str):
            recipe_build_id (Union[None, Unset, str]):
     """

    mapping_id: str
    plan_digest: str
    request_key: str
    recipe_build_id: Union[None, Unset, str] = UNSET





    def to_dict(self) -> dict[str, Any]:
        mapping_id = self.mapping_id

        plan_digest = self.plan_digest

        request_key = self.request_key

        recipe_build_id: Union[None, Unset, str]
        if isinstance(self.recipe_build_id, Unset):
            recipe_build_id = UNSET
        else:
            recipe_build_id = self.recipe_build_id


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "mapping_id": mapping_id,
            "plan_digest": plan_digest,
            "request_key": request_key,
        })
        if recipe_build_id is not UNSET:
            field_dict["recipe_build_id"] = recipe_build_id

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        mapping_id = d.pop("mapping_id")

        plan_digest = d.pop("plan_digest")

        request_key = d.pop("request_key")

        def _parse_recipe_build_id(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        recipe_build_id = _parse_recipe_build_id(d.pop("recipe_build_id", UNSET))


        install_request = cls(
            mapping_id=mapping_id,
            plan_digest=plan_digest,
            request_key=request_key,
            recipe_build_id=recipe_build_id,
        )

        return install_request
