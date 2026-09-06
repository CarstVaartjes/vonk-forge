from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.recipe_release_change_kind import check_recipe_release_change_kind
from ..models.recipe_release_change_kind import RecipeReleaseChangeKind
from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Union






T = TypeVar("T", bound="RecipeReleaseChange")



@_attrs_define
class RecipeReleaseChange:
    """
        Attributes:
            kind (RecipeReleaseChangeKind):
            summary (str):
            details (Union[None, Unset, str]):
            references (Union[Unset, list[str]]):
     """

    kind: RecipeReleaseChangeKind
    summary: str
    details: Union[None, Unset, str] = UNSET
    references: Union[Unset, list[str]] = UNSET





    def to_dict(self) -> dict[str, Any]:
        kind: str = self.kind

        summary = self.summary

        details: Union[None, Unset, str]
        if isinstance(self.details, Unset):
            details = UNSET
        else:
            details = self.details

        references: Union[Unset, list[str]] = UNSET
        if not isinstance(self.references, Unset):
            references = self.references




        field_dict: dict[str, Any] = {}

        field_dict.update({
            "kind": kind,
            "summary": summary,
        })
        if details is not UNSET:
            field_dict["details"] = details
        if references is not UNSET:
            field_dict["references"] = references

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        kind = check_recipe_release_change_kind(d.pop("kind"))




        summary = d.pop("summary")

        def _parse_details(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        details = _parse_details(d.pop("details", UNSET))


        references = cast(list[str], d.pop("references", UNSET))


        recipe_release_change = cls(
            kind=kind,
            summary=summary,
            details=details,
            references=references,
        )

        return recipe_release_change
