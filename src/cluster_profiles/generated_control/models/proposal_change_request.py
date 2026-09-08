from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast

if TYPE_CHECKING:
  from ..models.proposal_change_request_document import ProposalChangeRequestDocument





T = TypeVar("T", bound="ProposalChangeRequest")



@_attrs_define
class ProposalChangeRequest:
    """ Canonical persisted and API proposal change envelope.

        Attributes:
            document (ProposalChangeRequestDocument):
            path (str):
     """

    document: 'ProposalChangeRequestDocument'
    path: str





    def to_dict(self) -> dict[str, Any]:
        from ..models.proposal_change_request_document import ProposalChangeRequestDocument
        document = self.document.to_dict()

        path = self.path


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "document": document,
            "path": path,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.proposal_change_request_document import ProposalChangeRequestDocument
        d = dict(src_dict)
        document = ProposalChangeRequestDocument.from_dict(d.pop("document"))




        path = d.pop("path")

        proposal_change_request = cls(
            document=document,
            path=path,
        )

        return proposal_change_request
