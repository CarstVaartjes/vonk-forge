from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import Literal, cast






T = TypeVar("T", bound="ChangeResponse")



@_attrs_define
class ChangeResponse:
    """
        Attributes:
            authority_revision (str):
            mode (Literal['database']):
            previous_revision (str):
            proposal_digest (str):
     """

    authority_revision: str
    mode: Literal['database']
    previous_revision: str
    proposal_digest: str





    def to_dict(self) -> dict[str, Any]:
        authority_revision = self.authority_revision

        mode = self.mode

        previous_revision = self.previous_revision

        proposal_digest = self.proposal_digest


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "authority_revision": authority_revision,
            "mode": mode,
            "previous_revision": previous_revision,
            "proposal_digest": proposal_digest,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        authority_revision = d.pop("authority_revision")

        mode = cast(Literal['database'] , d.pop("mode"))
        if mode != 'database':
            raise ValueError(f"mode must match const 'database', got '{mode}'")

        previous_revision = d.pop("previous_revision")

        proposal_digest = d.pop("proposal_digest")

        change_response = cls(
            authority_revision=authority_revision,
            mode=mode,
            previous_revision=previous_revision,
            proposal_digest=proposal_digest,
        )

        return change_response
