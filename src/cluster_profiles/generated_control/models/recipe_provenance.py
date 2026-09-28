from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.recipe_provenance_source_kind import check_recipe_provenance_source_kind
from ..models.recipe_provenance_source_kind import RecipeProvenanceSourceKind
from ..types import UNSET, Unset
from typing import cast






T = TypeVar("T", bound="RecipeProvenance")



@_attrs_define
class RecipeProvenance:
    """
        Attributes:
            attribution (list[str]):
            source_kind (RecipeProvenanceSourceKind):
            source_reference (None | str | Unset):
     """

    attribution: list[str]
    source_kind: RecipeProvenanceSourceKind
    source_reference: None | str | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        attribution = self.attribution



        source_kind: str = self.source_kind

        source_reference: None | str | Unset
        if isinstance(self.source_reference, Unset):
            source_reference = UNSET
        else:
            source_reference = self.source_reference


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "attribution": attribution,
            "source_kind": source_kind,
        })
        if source_reference is not UNSET:
            field_dict["source_reference"] = source_reference

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        attribution = cast(list[str], d.pop("attribution"))


        source_kind = check_recipe_provenance_source_kind(d.pop("source_kind"))




        def _parse_source_reference(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        source_reference = _parse_source_reference(d.pop("source_reference", UNSET))


        recipe_provenance = cls(
            attribution=attribution,
            source_kind=source_kind,
            source_reference=source_reference,
        )

        return recipe_provenance
