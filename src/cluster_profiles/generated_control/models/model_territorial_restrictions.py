from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast






T = TypeVar("T", bound="ModelTerritorialRestrictions")



@_attrs_define
class ModelTerritorialRestrictions:
    """
        Attributes:
            denied_jurisdictions (list[str]):
            notice (str):
     """

    denied_jurisdictions: list[str]
    notice: str





    def to_dict(self) -> dict[str, Any]:
        denied_jurisdictions = self.denied_jurisdictions



        notice = self.notice


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "denied_jurisdictions": denied_jurisdictions,
            "notice": notice,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        denied_jurisdictions = cast(list[str], d.pop("denied_jurisdictions"))


        notice = d.pop("notice")

        model_territorial_restrictions = cls(
            denied_jurisdictions=denied_jurisdictions,
            notice=notice,
        )

        return model_territorial_restrictions
