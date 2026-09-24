from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
from typing import Literal, Union, cast
from typing import Union






T = TypeVar("T", bound="RecipeUpdateRequest")



@_attrs_define
class RecipeUpdateRequest:
    """
        Attributes:
            request_key (str):
            all_ (Union[Unset, bool]):  Default: False.
            schema_version (Union[Literal[2], Unset]):  Default: 2.
            selectors (Union[Unset, list[str]]):
     """

    request_key: str
    all_: Union[Unset, bool] = False
    schema_version: Union[Literal[2], Unset] = 2
    selectors: Union[Unset, list[str]] = UNSET





    def to_dict(self) -> dict[str, Any]:
        request_key = self.request_key

        all_ = self.all_

        schema_version = self.schema_version

        selectors: Union[Unset, list[str]] = UNSET
        if not isinstance(self.selectors, Unset):
            selectors = self.selectors




        field_dict: dict[str, Any] = {}

        field_dict.update({
            "request_key": request_key,
        })
        if all_ is not UNSET:
            field_dict["all"] = all_
        if schema_version is not UNSET:
            field_dict["schema_version"] = schema_version
        if selectors is not UNSET:
            field_dict["selectors"] = selectors

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        request_key = d.pop("request_key")

        all_ = d.pop("all", UNSET)

        schema_version = cast(Union[Literal[2], Unset] , d.pop("schema_version", UNSET))
        if schema_version != 2 and not isinstance(schema_version, Unset):
            raise ValueError(f"schema_version must match const 2, got '{schema_version}'")

        selectors = cast(list[str], d.pop("selectors", UNSET))


        recipe_update_request = cls(
            request_key=request_key,
            all_=all_,
            schema_version=schema_version,
            selectors=selectors,
        )

        return recipe_update_request
