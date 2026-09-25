from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.mapping_selection_action import check_mapping_selection_action
from ..models.mapping_selection_action import MappingSelectionAction
from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Union

if TYPE_CHECKING:
  from ..models.spark_group_node import SparkGroupNode
  from ..models.mapping_selection_parameters import MappingSelectionParameters





T = TypeVar("T", bound="MappingSelection")



@_attrs_define
class MappingSelection:
    """
        Attributes:
            action (MappingSelectionAction):
            mapping_id (Union[None, str]):
            nodes (list['SparkGroupNode']):
            placement_digest (str):
            topology_name (str):
            mapping_generation (Union[None, Unset, int]):
            parameters (Union[Unset, MappingSelectionParameters]):
     """

    action: MappingSelectionAction
    mapping_id: Union[None, str]
    nodes: list['SparkGroupNode']
    placement_digest: str
    topology_name: str
    mapping_generation: Union[None, Unset, int] = UNSET
    parameters: Union[Unset, 'MappingSelectionParameters'] = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.spark_group_node import SparkGroupNode
        from ..models.mapping_selection_parameters import MappingSelectionParameters
        action: str = self.action

        mapping_id: Union[None, str]
        mapping_id = self.mapping_id

        nodes = []
        for nodes_item_data in self.nodes:
            nodes_item = nodes_item_data.to_dict()
            nodes.append(nodes_item)



        placement_digest = self.placement_digest

        topology_name = self.topology_name

        mapping_generation: Union[None, Unset, int]
        if isinstance(self.mapping_generation, Unset):
            mapping_generation = UNSET
        else:
            mapping_generation = self.mapping_generation

        parameters: Union[Unset, dict[str, Any]] = UNSET
        if not isinstance(self.parameters, Unset):
            parameters = self.parameters.to_dict()


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "action": action,
            "mapping_id": mapping_id,
            "nodes": nodes,
            "placement_digest": placement_digest,
            "topology_name": topology_name,
        })
        if mapping_generation is not UNSET:
            field_dict["mapping_generation"] = mapping_generation
        if parameters is not UNSET:
            field_dict["parameters"] = parameters

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.spark_group_node import SparkGroupNode
        from ..models.mapping_selection_parameters import MappingSelectionParameters
        d = dict(src_dict)
        action = check_mapping_selection_action(d.pop("action"))




        def _parse_mapping_id(data: object) -> Union[None, str]:
            if data is None:
                return data
            return cast(Union[None, str], data)

        mapping_id = _parse_mapping_id(d.pop("mapping_id"))


        nodes = []
        _nodes = d.pop("nodes")
        for nodes_item_data in (_nodes):
            nodes_item = SparkGroupNode.from_dict(nodes_item_data)



            nodes.append(nodes_item)


        placement_digest = d.pop("placement_digest")

        topology_name = d.pop("topology_name")

        def _parse_mapping_generation(data: object) -> Union[None, Unset, int]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, int], data)

        mapping_generation = _parse_mapping_generation(d.pop("mapping_generation", UNSET))


        _parameters = d.pop("parameters", UNSET)
        parameters: Union[Unset, MappingSelectionParameters]
        if isinstance(_parameters,  Unset):
            parameters = UNSET
        else:
            parameters = MappingSelectionParameters.from_dict(_parameters)




        mapping_selection = cls(
            action=action,
            mapping_id=mapping_id,
            nodes=nodes,
            placement_digest=placement_digest,
            topology_name=topology_name,
            mapping_generation=mapping_generation,
            parameters=parameters,
        )

        return mapping_selection
