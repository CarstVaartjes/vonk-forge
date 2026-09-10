from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import Literal, Union, cast
from typing import Union






T = TypeVar("T", bound="RecipeOperatorRequest")



@_attrs_define
class RecipeOperatorRequest:
    """
        Attributes:
            request_key (str):
            schema_version (Union[Literal[2], Unset]):  Default: 2.
            with_model (Union[Unset, bool]):  Default: False.
     """

    request_key: str
    schema_version: Union[Literal[2], Unset] = 2
    with_model: Union[Unset, bool] = False





    def to_dict(self) -> dict[str, Any]:
        request_key = self.request_key

        schema_version = self.schema_version

        with_model = self.with_model


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "request_key": request_key,
        })
        if schema_version is not UNSET:
            field_dict["schema_version"] = schema_version
        if with_model is not UNSET:
            field_dict["with_model"] = with_model

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        request_key = d.pop("request_key")

        schema_version = cast(Union[Literal[2], Unset] , d.pop("schema_version", UNSET))
        if schema_version != 2 and not isinstance(schema_version, Unset):
            raise ValueError(f"schema_version must match const 2, got '{schema_version}'")

        with_model = d.pop("with_model", UNSET)

        recipe_operator_request = cls(
            request_key=request_key,
            schema_version=schema_version,
            with_model=with_model,
        )

        return recipe_operator_request
