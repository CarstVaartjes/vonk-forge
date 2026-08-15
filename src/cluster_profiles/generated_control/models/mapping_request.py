from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
from typing import Union

if TYPE_CHECKING:
  from ..models.mapping_request_parameters import MappingRequestParameters





T = TypeVar("T", bound="MappingRequest")



@_attrs_define
class MappingRequest:
    """
        Attributes:
            node_ids (list[str]):
            placement_digest (str):
            recipe_revision_id (str):
            request_key (str):
            parameters (Union[Unset, MappingRequestParameters]):
     """

    node_ids: list[str]
    placement_digest: str
    recipe_revision_id: str
    request_key: str
    parameters: Union[Unset, 'MappingRequestParameters'] = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.mapping_request_parameters import MappingRequestParameters
        node_ids = self.node_ids



        placement_digest = self.placement_digest

        recipe_revision_id = self.recipe_revision_id

        request_key = self.request_key

        parameters: Union[Unset, dict[str, Any]] = UNSET
        if not isinstance(self.parameters, Unset):
            parameters = self.parameters.to_dict()


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "node_ids": node_ids,
            "placement_digest": placement_digest,
            "recipe_revision_id": recipe_revision_id,
            "request_key": request_key,
        })
        if parameters is not UNSET:
            field_dict["parameters"] = parameters

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.mapping_request_parameters import MappingRequestParameters
        d = dict(src_dict)
        node_ids = cast(list[str], d.pop("node_ids"))


        placement_digest = d.pop("placement_digest")

        recipe_revision_id = d.pop("recipe_revision_id")

        request_key = d.pop("request_key")

        _parameters = d.pop("parameters", UNSET)
        parameters: Union[Unset, MappingRequestParameters]
        if isinstance(_parameters,  Unset):
            parameters = UNSET
        else:
            parameters = MappingRequestParameters.from_dict(_parameters)




        mapping_request = cls(
            node_ids=node_ids,
            placement_digest=placement_digest,
            recipe_revision_id=recipe_revision_id,
            request_key=request_key,
            parameters=parameters,
        )

        return mapping_request
