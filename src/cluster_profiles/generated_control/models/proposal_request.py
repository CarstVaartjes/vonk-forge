from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast

if TYPE_CHECKING:
  from ..models.proposal_change_request import ProposalChangeRequest





T = TypeVar("T", bound="ProposalRequest")



@_attrs_define
class ProposalRequest:
    """
        Attributes:
            base_revision (str):
            changes (list['ProposalChangeRequest']):
     """

    base_revision: str
    changes: list['ProposalChangeRequest']





    def to_dict(self) -> dict[str, Any]:
        from ..models.proposal_change_request import ProposalChangeRequest
        base_revision = self.base_revision

        changes = []
        for changes_item_data in self.changes:
            changes_item = changes_item_data.to_dict()
            changes.append(changes_item)




        field_dict: dict[str, Any] = {}

        field_dict.update({
            "base_revision": base_revision,
            "changes": changes,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.proposal_change_request import ProposalChangeRequest
        d = dict(src_dict)
        base_revision = d.pop("base_revision")

        changes = []
        _changes = d.pop("changes")
        for changes_item_data in (_changes):
            changes_item = ProposalChangeRequest.from_dict(changes_item_data)



            changes.append(changes_item)


        proposal_request = cls(
            base_revision=base_revision,
            changes=changes,
        )

        return proposal_request
