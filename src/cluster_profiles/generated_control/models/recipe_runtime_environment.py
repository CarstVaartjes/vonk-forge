from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast






T = TypeVar("T", bound="RecipeRuntimeEnvironment")



@_attrs_define
class RecipeRuntimeEnvironment:
    """
        Attributes:
            name (str):
            secret (None | str | Unset):
            value (bool | float | int | None | str | Unset):
     """

    name: str
    secret: None | str | Unset = UNSET
    value: bool | float | int | None | str | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        name = self.name

        secret: None | str | Unset
        if isinstance(self.secret, Unset):
            secret = UNSET
        else:
            secret = self.secret

        value: bool | float | int | None | str | Unset
        if isinstance(self.value, Unset):
            value = UNSET
        else:
            value = self.value


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "name": name,
        })
        if secret is not UNSET:
            field_dict["secret"] = secret
        if value is not UNSET:
            field_dict["value"] = value

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        name = d.pop("name")

        def _parse_secret(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        secret = _parse_secret(d.pop("secret", UNSET))


        def _parse_value(data: object) -> bool | float | int | None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(bool | float | int | None | str | Unset, data)

        value = _parse_value(d.pop("value", UNSET))


        recipe_runtime_environment = cls(
            name=name,
            secret=secret,
            value=value,
        )

        return recipe_runtime_environment
