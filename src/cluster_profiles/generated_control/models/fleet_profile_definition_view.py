from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Literal, Union, cast

if TYPE_CHECKING:
  from ..models.fleet_profile_definition import FleetProfileDefinition





T = TypeVar("T", bound="FleetProfileDefinitionView")



@_attrs_define
class FleetProfileDefinitionView:
    """
        Attributes:
            definition (FleetProfileDefinition): Saved authoring intent, independent of execution and cache projections.
            id (Union[None, str]):
            number (int):
            revision (int):
            schema_version (Union[Literal[2], Unset]):  Default: 2.
     """

    definition: 'FleetProfileDefinition'
    id: Union[None, str]
    number: int
    revision: int
    schema_version: Union[Literal[2], Unset] = 2





    def to_dict(self) -> dict[str, Any]:
        from ..models.fleet_profile_definition import FleetProfileDefinition
        definition = self.definition.to_dict()

        id: Union[None, str]
        id = self.id

        number = self.number

        revision = self.revision

        schema_version = self.schema_version


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "definition": definition,
            "id": id,
            "number": number,
            "revision": revision,
        })
        if schema_version is not UNSET:
            field_dict["schema_version"] = schema_version

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.fleet_profile_definition import FleetProfileDefinition
        d = dict(src_dict)
        definition = FleetProfileDefinition.from_dict(d.pop("definition"))




        def _parse_id(data: object) -> Union[None, str]:
            if data is None:
                return data
            return cast(Union[None, str], data)

        id = _parse_id(d.pop("id"))


        number = d.pop("number")

        revision = d.pop("revision")

        schema_version = cast(Union[Literal[2], Unset] , d.pop("schema_version", UNSET))
        if schema_version != 2 and not isinstance(schema_version, Unset):
            raise ValueError(f"schema_version must match const 2, got '{schema_version}'")

        fleet_profile_definition_view = cls(
            definition=definition,
            id=id,
            number=number,
            revision=revision,
            schema_version=schema_version,
        )

        return fleet_profile_definition_view
