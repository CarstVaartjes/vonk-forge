from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.recipe_metadata_alignment_type_0 import check_recipe_metadata_alignment_type_0
from ..models.recipe_metadata_alignment_type_0 import RecipeMetadataAlignmentType0
from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Union






T = TypeVar("T", bound="RecipeMetadata")



@_attrs_define
class RecipeMetadata:
    """
        Attributes:
            description (str):
            tags (list[str]):
            title (str):
            alignment (Union[None, RecipeMetadataAlignmentType0, Unset]):
     """

    description: str
    tags: list[str]
    title: str
    alignment: Union[None, RecipeMetadataAlignmentType0, Unset] = UNSET





    def to_dict(self) -> dict[str, Any]:
        description = self.description

        tags = self.tags



        title = self.title

        alignment: Union[None, Unset, str]
        if isinstance(self.alignment, Unset):
            alignment = UNSET
        elif isinstance(self.alignment, str):
            alignment = self.alignment
        else:
            alignment = self.alignment


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "description": description,
            "tags": tags,
            "title": title,
        })
        if alignment is not UNSET:
            field_dict["alignment"] = alignment

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        description = d.pop("description")

        tags = cast(list[str], d.pop("tags"))


        title = d.pop("title")

        def _parse_alignment(data: object) -> Union[None, RecipeMetadataAlignmentType0, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                alignment_type_0 = check_recipe_metadata_alignment_type_0(data)



                return alignment_type_0
            except: # noqa: E722
                pass
            return cast(Union[None, RecipeMetadataAlignmentType0, Unset], data)

        alignment = _parse_alignment(d.pop("alignment", UNSET))


        recipe_metadata = cls(
            description=description,
            tags=tags,
            title=title,
            alignment=alignment,
        )

        return recipe_metadata
