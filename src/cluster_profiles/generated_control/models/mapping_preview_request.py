from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
from typing import Union

if TYPE_CHECKING:
  from ..models.mapping_preview_request_parameters import MappingPreviewRequestParameters





T = TypeVar("T", bound="MappingPreviewRequest")



@_attrs_define
class MappingPreviewRequest:
    """
        Attributes:
            node_ids (list[str]):
            recipe_revision_id (str):
            parameters (Union[Unset, MappingPreviewRequestParameters]):
     """

    node_ids: list[str]
    recipe_revision_id: str
    parameters: Union[Unset, 'MappingPreviewRequestParameters'] = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.mapping_preview_request_parameters import MappingPreviewRequestParameters
        node_ids = self.node_ids



        recipe_revision_id = self.recipe_revision_id

        parameters: Union[Unset, dict[str, Any]] = UNSET
        if not isinstance(self.parameters, Unset):
            parameters = self.parameters.to_dict()


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "node_ids": node_ids,
            "recipe_revision_id": recipe_revision_id,
        })
        if parameters is not UNSET:
            field_dict["parameters"] = parameters

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.mapping_preview_request_parameters import MappingPreviewRequestParameters
        d = dict(src_dict)
        node_ids = cast(list[str], d.pop("node_ids"))


        recipe_revision_id = d.pop("recipe_revision_id")

        _parameters = d.pop("parameters", UNSET)
        parameters: Union[Unset, MappingPreviewRequestParameters]
        if isinstance(_parameters,  Unset):
            parameters = UNSET
        else:
            parameters = MappingPreviewRequestParameters.from_dict(_parameters)




        mapping_preview_request = cls(
            node_ids=node_ids,
            recipe_revision_id=recipe_revision_id,
            parameters=parameters,
        )

        return mapping_preview_request
