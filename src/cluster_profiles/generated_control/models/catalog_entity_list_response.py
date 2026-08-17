from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Union

if TYPE_CHECKING:
  from ..models.catalog_entity_revision_response import CatalogEntityRevisionResponse





T = TypeVar("T", bound="CatalogEntityListResponse")



@_attrs_define
class CatalogEntityListResponse:
    """
        Attributes:
            entities (list['CatalogEntityRevisionResponse']):
            next_cursor (Union[None, Unset, str]):
     """

    entities: list['CatalogEntityRevisionResponse']
    next_cursor: Union[None, Unset, str] = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.catalog_entity_revision_response import CatalogEntityRevisionResponse
        entities = []
        for entities_item_data in self.entities:
            entities_item = entities_item_data.to_dict()
            entities.append(entities_item)



        next_cursor: Union[None, Unset, str]
        if isinstance(self.next_cursor, Unset):
            next_cursor = UNSET
        else:
            next_cursor = self.next_cursor


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "entities": entities,
        })
        if next_cursor is not UNSET:
            field_dict["next_cursor"] = next_cursor

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.catalog_entity_revision_response import CatalogEntityRevisionResponse
        d = dict(src_dict)
        entities = []
        _entities = d.pop("entities")
        for entities_item_data in (_entities):
            entities_item = CatalogEntityRevisionResponse.from_dict(entities_item_data)



            entities.append(entities_item)


        def _parse_next_cursor(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        next_cursor = _parse_next_cursor(d.pop("next_cursor", UNSET))


        catalog_entity_list_response = cls(
            entities=entities,
            next_cursor=next_cursor,
        )

        return catalog_entity_list_response
