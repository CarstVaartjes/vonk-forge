from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast, Union
from typing import Union






T = TypeVar("T", bound="ManagedCatalogSyncProblem")



@_attrs_define
class ManagedCatalogSyncProblem:
    """
        Attributes:
            code (str):
            detail (str):
            recipe_uri (Union[None, Unset, str]):
     """

    code: str
    detail: str
    recipe_uri: Union[None, Unset, str] = UNSET





    def to_dict(self) -> dict[str, Any]:
        code = self.code

        detail = self.detail

        recipe_uri: Union[None, Unset, str]
        if isinstance(self.recipe_uri, Unset):
            recipe_uri = UNSET
        else:
            recipe_uri = self.recipe_uri


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "code": code,
            "detail": detail,
        })
        if recipe_uri is not UNSET:
            field_dict["recipe_uri"] = recipe_uri

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        code = d.pop("code")

        detail = d.pop("detail")

        def _parse_recipe_uri(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        recipe_uri = _parse_recipe_uri(d.pop("recipe_uri", UNSET))


        managed_catalog_sync_problem = cls(
            code=code,
            detail=detail,
            recipe_uri=recipe_uri,
        )

        return managed_catalog_sync_problem
