from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.operational_mapping_state import check_operational_mapping_state
from ..models.operational_mapping_state import OperationalMappingState
from typing import cast

if TYPE_CHECKING:
  from ..models.operational_mapping_node import OperationalMappingNode





T = TypeVar("T", bound="OperationalMapping")



@_attrs_define
class OperationalMapping:
    """
        Attributes:
            generation (int):
            mapping_id (str):
            nodes (list['OperationalMappingNode']):
            recipe_revision_id (str):
            state (OperationalMappingState):
            topology_name (str):
     """

    generation: int
    mapping_id: str
    nodes: list['OperationalMappingNode']
    recipe_revision_id: str
    state: OperationalMappingState
    topology_name: str





    def to_dict(self) -> dict[str, Any]:
        from ..models.operational_mapping_node import OperationalMappingNode
        generation = self.generation

        mapping_id = self.mapping_id

        nodes = []
        for nodes_item_data in self.nodes:
            nodes_item = nodes_item_data.to_dict()
            nodes.append(nodes_item)



        recipe_revision_id = self.recipe_revision_id

        state: str = self.state

        topology_name = self.topology_name


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "generation": generation,
            "mapping_id": mapping_id,
            "nodes": nodes,
            "recipe_revision_id": recipe_revision_id,
            "state": state,
            "topology_name": topology_name,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.operational_mapping_node import OperationalMappingNode
        d = dict(src_dict)
        generation = d.pop("generation")

        mapping_id = d.pop("mapping_id")

        nodes = []
        _nodes = d.pop("nodes")
        for nodes_item_data in (_nodes):
            nodes_item = OperationalMappingNode.from_dict(nodes_item_data)



            nodes.append(nodes_item)


        recipe_revision_id = d.pop("recipe_revision_id")

        state = check_operational_mapping_state(d.pop("state"))




        topology_name = d.pop("topology_name")

        operational_mapping = cls(
            generation=generation,
            mapping_id=mapping_id,
            nodes=nodes,
            recipe_revision_id=recipe_revision_id,
            state=state,
            topology_name=topology_name,
        )

        return operational_mapping
