from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast

if TYPE_CHECKING:
  from ..models.node_status import NodeStatus





T = TypeVar("T", bound="FleetStatusResponse")



@_attrs_define
class FleetStatusResponse:
    """
        Attributes:
            authority_revision (str):
            evidence_digest (str):
            nodes (list['NodeStatus']):
     """

    authority_revision: str
    evidence_digest: str
    nodes: list['NodeStatus']





    def to_dict(self) -> dict[str, Any]:
        from ..models.node_status import NodeStatus
        authority_revision = self.authority_revision

        evidence_digest = self.evidence_digest

        nodes = []
        for nodes_item_data in self.nodes:
            nodes_item = nodes_item_data.to_dict()
            nodes.append(nodes_item)




        field_dict: dict[str, Any] = {}

        field_dict.update({
            "authority_revision": authority_revision,
            "evidence_digest": evidence_digest,
            "nodes": nodes,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.node_status import NodeStatus
        d = dict(src_dict)
        authority_revision = d.pop("authority_revision")

        evidence_digest = d.pop("evidence_digest")

        nodes = []
        _nodes = d.pop("nodes")
        for nodes_item_data in (_nodes):
            nodes_item = NodeStatus.from_dict(nodes_item_data)



            nodes.append(nodes_item)


        fleet_status_response = cls(
            authority_revision=authority_revision,
            evidence_digest=evidence_digest,
            nodes=nodes,
        )

        return fleet_status_response
