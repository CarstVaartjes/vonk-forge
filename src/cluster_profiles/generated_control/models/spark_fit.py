from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
from typing import Union

if TYPE_CHECKING:
  from ..models.run_switch_reason import RunSwitchReason
  from ..models.spark_fit_node import SparkFitNode





T = TypeVar("T", bound="SparkFit")



@_attrs_define
class SparkFit:
    """
        Attributes:
            allowed (bool):
            nodes (list['SparkFitNode']):
            blockers (Union[Unset, list['RunSwitchReason']]):
            warnings (Union[Unset, list['RunSwitchReason']]):
     """

    allowed: bool
    nodes: list['SparkFitNode']
    blockers: Union[Unset, list['RunSwitchReason']] = UNSET
    warnings: Union[Unset, list['RunSwitchReason']] = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.run_switch_reason import RunSwitchReason
        from ..models.spark_fit_node import SparkFitNode
        allowed = self.allowed

        nodes = []
        for nodes_item_data in self.nodes:
            nodes_item = nodes_item_data.to_dict()
            nodes.append(nodes_item)



        blockers: Union[Unset, list[dict[str, Any]]] = UNSET
        if not isinstance(self.blockers, Unset):
            blockers = []
            for blockers_item_data in self.blockers:
                blockers_item = blockers_item_data.to_dict()
                blockers.append(blockers_item)



        warnings: Union[Unset, list[dict[str, Any]]] = UNSET
        if not isinstance(self.warnings, Unset):
            warnings = []
            for warnings_item_data in self.warnings:
                warnings_item = warnings_item_data.to_dict()
                warnings.append(warnings_item)




        field_dict: dict[str, Any] = {}

        field_dict.update({
            "allowed": allowed,
            "nodes": nodes,
        })
        if blockers is not UNSET:
            field_dict["blockers"] = blockers
        if warnings is not UNSET:
            field_dict["warnings"] = warnings

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.run_switch_reason import RunSwitchReason
        from ..models.spark_fit_node import SparkFitNode
        d = dict(src_dict)
        allowed = d.pop("allowed")

        nodes = []
        _nodes = d.pop("nodes")
        for nodes_item_data in (_nodes):
            nodes_item = SparkFitNode.from_dict(nodes_item_data)



            nodes.append(nodes_item)


        blockers = []
        _blockers = d.pop("blockers", UNSET)
        for blockers_item_data in (_blockers or []):
            blockers_item = RunSwitchReason.from_dict(blockers_item_data)



            blockers.append(blockers_item)


        warnings = []
        _warnings = d.pop("warnings", UNSET)
        for warnings_item_data in (_warnings or []):
            warnings_item = RunSwitchReason.from_dict(warnings_item_data)



            warnings.append(warnings_item)


        spark_fit = cls(
            allowed=allowed,
            nodes=nodes,
            blockers=blockers,
            warnings=warnings,
        )

        return spark_fit
