from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast

if TYPE_CHECKING:
  from ..models.library_projection_reason import LibraryProjectionReason





T = TypeVar("T", bound="RejectedNode")



@_attrs_define
class RejectedNode:
    """
        Attributes:
            node_id (str):
            reasons (list['LibraryProjectionReason']):
     """

    node_id: str
    reasons: list['LibraryProjectionReason']





    def to_dict(self) -> dict[str, Any]:
        from ..models.library_projection_reason import LibraryProjectionReason
        node_id = self.node_id

        reasons = []
        for reasons_item_data in self.reasons:
            reasons_item = reasons_item_data.to_dict()
            reasons.append(reasons_item)




        field_dict: dict[str, Any] = {}

        field_dict.update({
            "node_id": node_id,
            "reasons": reasons,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.library_projection_reason import LibraryProjectionReason
        d = dict(src_dict)
        node_id = d.pop("node_id")

        reasons = []
        _reasons = d.pop("reasons")
        for reasons_item_data in (_reasons):
            reasons_item = LibraryProjectionReason.from_dict(reasons_item_data)



            reasons.append(reasons_item)


        rejected_node = cls(
            node_id=node_id,
            reasons=reasons,
        )

        return rejected_node
