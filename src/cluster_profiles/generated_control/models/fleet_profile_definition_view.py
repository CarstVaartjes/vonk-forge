from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast

if TYPE_CHECKING:
  from ..models.fleet_profile_definition import FleetProfileDefinition





T = TypeVar("T", bound="FleetProfileDefinitionView")



@_attrs_define
class FleetProfileDefinitionView:
    """
        Attributes:
            definition (FleetProfileDefinition): Saved authoring intent, independent of execution and cache projections.
            id (None | str):
            number (int):
            revision (int):
     """

    definition: FleetProfileDefinition
    id: None | str
    number: int
    revision: int





    def to_dict(self) -> dict[str, Any]:
        from ..models.fleet_profile_definition import FleetProfileDefinition # noqa: PLC0415
        definition = self.definition.to_dict()

        id: None | str
        id = self.id

        number = self.number

        revision = self.revision


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "definition": definition,
            "id": id,
            "number": number,
            "revision": revision,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.fleet_profile_definition import FleetProfileDefinition # noqa: PLC0415
        d = dict(src_dict)
        definition = FleetProfileDefinition.from_dict(d.pop("definition"))




        def _parse_id(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        id = _parse_id(d.pop("id"))


        number = d.pop("number")

        revision = d.pop("revision")

        fleet_profile_definition_view = cls(
            definition=definition,
            id=id,
            number=number,
            revision=revision,
        )

        return fleet_profile_definition_view
