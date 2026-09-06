from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast

if TYPE_CHECKING:
  from ..models.identity_history_item import IdentityHistoryItem





T = TypeVar("T", bound="IdentityHistoryResponse")



@_attrs_define
class IdentityHistoryResponse:
    """
        Attributes:
            identities (list['IdentityHistoryItem']):
     """

    identities: list['IdentityHistoryItem']





    def to_dict(self) -> dict[str, Any]:
        from ..models.identity_history_item import IdentityHistoryItem
        identities = []
        for identities_item_data in self.identities:
            identities_item = identities_item_data.to_dict()
            identities.append(identities_item)




        field_dict: dict[str, Any] = {}

        field_dict.update({
            "identities": identities,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.identity_history_item import IdentityHistoryItem
        d = dict(src_dict)
        identities = []
        _identities = d.pop("identities")
        for identities_item_data in (_identities):
            identities_item = IdentityHistoryItem.from_dict(identities_item_data)



            identities.append(identities_item)


        identity_history_response = cls(
            identities=identities,
        )

        return identity_history_response
