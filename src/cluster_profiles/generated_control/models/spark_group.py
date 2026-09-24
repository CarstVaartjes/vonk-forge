from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast

if TYPE_CHECKING:
  from ..models.spark_group_node import SparkGroupNode





T = TypeVar("T", bound="SparkGroup")



@_attrs_define
class SparkGroup:
    """ A complete, rank-labelled Spark group selected by the operator.

        Attributes:
            nodes (list['SparkGroupNode']):
     """

    nodes: list['SparkGroupNode']





    def to_dict(self) -> dict[str, Any]:
        from ..models.spark_group_node import SparkGroupNode
        nodes = []
        for nodes_item_data in self.nodes:
            nodes_item = nodes_item_data.to_dict()
            nodes.append(nodes_item)




        field_dict: dict[str, Any] = {}

        field_dict.update({
            "nodes": nodes,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.spark_group_node import SparkGroupNode
        d = dict(src_dict)
        nodes = []
        _nodes = d.pop("nodes")
        for nodes_item_data in (_nodes):
            nodes_item = SparkGroupNode.from_dict(nodes_item_data)



            nodes.append(nodes_item)


        spark_group = cls(
            nodes=nodes,
        )

        return spark_group
