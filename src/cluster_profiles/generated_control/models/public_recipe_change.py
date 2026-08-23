from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.public_recipe_change_kind import check_public_recipe_change_kind
from ..models.public_recipe_change_kind import PublicRecipeChangeKind
from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Union






T = TypeVar("T", bound="PublicRecipeChange")



@_attrs_define
class PublicRecipeChange:
    """
        Attributes:
            kind (PublicRecipeChangeKind):
            references (list[str]):
            summary (str):
            details (Union[None, Unset, str]):
     """

    kind: PublicRecipeChangeKind
    references: list[str]
    summary: str
    details: Union[None, Unset, str] = UNSET





    def to_dict(self) -> dict[str, Any]:
        kind: str = self.kind

        references = self.references



        summary = self.summary

        details: Union[None, Unset, str]
        if isinstance(self.details, Unset):
            details = UNSET
        else:
            details = self.details


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "kind": kind,
            "references": references,
            "summary": summary,
        })
        if details is not UNSET:
            field_dict["details"] = details

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        kind = check_public_recipe_change_kind(d.pop("kind"))




        references = cast(list[str], d.pop("references"))


        summary = d.pop("summary")

        def _parse_details(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        details = _parse_details(d.pop("details", UNSET))


        public_recipe_change = cls(
            kind=kind,
            references=references,
            summary=summary,
            details=details,
        )

        return public_recipe_change
