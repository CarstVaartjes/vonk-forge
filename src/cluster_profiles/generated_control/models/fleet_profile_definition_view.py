from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast

if TYPE_CHECKING:
  from ..models.fleet_profile_definition import FleetProfileDefinition
  from ..models.saved_profile_projection_issue import SavedProfileProjectionIssue





T = TypeVar("T", bound="FleetProfileDefinitionView")



@_attrs_define
class FleetProfileDefinitionView:
    """
        Attributes:
            definition (FleetProfileDefinition | None):
            id (None | str):
            number (int):
            revision (int):
            projection_issue (None | SavedProfileProjectionIssue | Unset):
     """

    definition: FleetProfileDefinition | None
    id: None | str
    number: int
    revision: int
    projection_issue: None | SavedProfileProjectionIssue | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.fleet_profile_definition import FleetProfileDefinition # noqa: PLC0415
        from ..models.saved_profile_projection_issue import SavedProfileProjectionIssue # noqa: PLC0415
        definition: dict[str, Any] | None
        if isinstance(self.definition, FleetProfileDefinition):
            definition = self.definition.to_dict()
        else:
            definition = self.definition

        id: None | str
        id = self.id

        number = self.number

        revision = self.revision

        projection_issue: dict[str, Any] | None | Unset
        if isinstance(self.projection_issue, Unset):
            projection_issue = UNSET
        elif isinstance(self.projection_issue, SavedProfileProjectionIssue):
            projection_issue = self.projection_issue.to_dict()
        else:
            projection_issue = self.projection_issue


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "definition": definition,
            "id": id,
            "number": number,
            "revision": revision,
        })
        if projection_issue is not UNSET:
            field_dict["projection_issue"] = projection_issue

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.fleet_profile_definition import FleetProfileDefinition # noqa: PLC0415
        from ..models.saved_profile_projection_issue import SavedProfileProjectionIssue # noqa: PLC0415
        d = dict(src_dict)
        def _parse_definition(data: object) -> FleetProfileDefinition | None:
            if data is None:
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                definition_type_0 = FleetProfileDefinition.from_dict(data)



                return definition_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(FleetProfileDefinition | None, data)

        definition = _parse_definition(d.pop("definition"))


        def _parse_id(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        id = _parse_id(d.pop("id"))


        number = d.pop("number")

        revision = d.pop("revision")

        def _parse_projection_issue(data: object) -> None | SavedProfileProjectionIssue | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                projection_issue_type_0 = SavedProfileProjectionIssue.from_dict(data)



                return projection_issue_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | SavedProfileProjectionIssue | Unset, data)

        projection_issue = _parse_projection_issue(d.pop("projection_issue", UNSET))


        fleet_profile_definition_view = cls(
            definition=definition,
            id=id,
            number=number,
            revision=revision,
            projection_issue=projection_issue,
        )

        return fleet_profile_definition_view
