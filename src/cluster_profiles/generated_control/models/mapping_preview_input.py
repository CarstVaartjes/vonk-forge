from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast

if TYPE_CHECKING:
  from ..models.mapping_preview_input_parameters import MappingPreviewInputParameters





T = TypeVar("T", bound="MappingPreviewInput")



@_attrs_define
class MappingPreviewInput:
    """
        Attributes:
            node_ids (list[str]):
            parameters (MappingPreviewInputParameters):
            recipe_revision_id (str):
     """

    node_ids: list[str]
    parameters: 'MappingPreviewInputParameters'
    recipe_revision_id: str





    def to_dict(self) -> dict[str, Any]:
        from ..models.mapping_preview_input_parameters import MappingPreviewInputParameters
        node_ids = self.node_ids



        parameters = self.parameters.to_dict()

        recipe_revision_id = self.recipe_revision_id


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "node_ids": node_ids,
            "parameters": parameters,
            "recipe_revision_id": recipe_revision_id,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.mapping_preview_input_parameters import MappingPreviewInputParameters
        d = dict(src_dict)
        node_ids = cast(list[str], d.pop("node_ids"))


        parameters = MappingPreviewInputParameters.from_dict(d.pop("parameters"))




        recipe_revision_id = d.pop("recipe_revision_id")

        mapping_preview_input = cls(
            node_ids=node_ids,
            parameters=parameters,
            recipe_revision_id=recipe_revision_id,
        )

        return mapping_preview_input
