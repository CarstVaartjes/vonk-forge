from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast

if TYPE_CHECKING:
  from ..models.agent_operation_change import AgentOperationChange
  from ..models.installation_node_change import InstallationNodeChange
  from ..models.job_change import JobChange
  from ..models.node_profile_change import NodeProfileChange
  from ..models.recipe_installation_change import RecipeInstallationChange
  from ..models.recipe_run_change import RecipeRunChange
  from ..models.run_node_change import RunNodeChange





T = TypeVar("T", bound="FleetChangeEvent")



@_attrs_define
class FleetChangeEvent:
    """
        Attributes:
            change (AgentOperationChange | InstallationNodeChange | JobChange | NodeProfileChange | RecipeInstallationChange
                | RecipeRunChange | RunNodeChange):
            event_cursor (int):
            projection_refresh_required (bool | Unset):  Default: True.
     """

    change: AgentOperationChange | InstallationNodeChange | JobChange | NodeProfileChange | RecipeInstallationChange | RecipeRunChange | RunNodeChange
    event_cursor: int
    projection_refresh_required: bool | Unset = True





    def to_dict(self) -> dict[str, Any]:
        from ..models.agent_operation_change import AgentOperationChange # noqa: PLC0415
        from ..models.installation_node_change import InstallationNodeChange # noqa: PLC0415
        from ..models.job_change import JobChange # noqa: PLC0415
        from ..models.node_profile_change import NodeProfileChange # noqa: PLC0415
        from ..models.recipe_installation_change import RecipeInstallationChange # noqa: PLC0415
        from ..models.recipe_run_change import RecipeRunChange # noqa: PLC0415
        from ..models.run_node_change import RunNodeChange # noqa: PLC0415
        change: dict[str, Any]
        if isinstance(self.change, NodeProfileChange):
            change = self.change.to_dict()
        elif isinstance(self.change, RecipeInstallationChange):
            change = self.change.to_dict()
        elif isinstance(self.change, InstallationNodeChange):
            change = self.change.to_dict()
        elif isinstance(self.change, RecipeRunChange):
            change = self.change.to_dict()
        elif isinstance(self.change, RunNodeChange):
            change = self.change.to_dict()
        elif isinstance(self.change, JobChange):
            change = self.change.to_dict()
        else:
            change = self.change.to_dict()


        event_cursor = self.event_cursor

        projection_refresh_required = self.projection_refresh_required


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "change": change,
            "event_cursor": event_cursor,
        })
        if projection_refresh_required is not UNSET:
            field_dict["projection_refresh_required"] = projection_refresh_required

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.agent_operation_change import AgentOperationChange # noqa: PLC0415
        from ..models.installation_node_change import InstallationNodeChange # noqa: PLC0415
        from ..models.job_change import JobChange # noqa: PLC0415
        from ..models.node_profile_change import NodeProfileChange # noqa: PLC0415
        from ..models.recipe_installation_change import RecipeInstallationChange # noqa: PLC0415
        from ..models.recipe_run_change import RecipeRunChange # noqa: PLC0415
        from ..models.run_node_change import RunNodeChange # noqa: PLC0415
        d = dict(src_dict)
        def _parse_change(data: object) -> AgentOperationChange | InstallationNodeChange | JobChange | NodeProfileChange | RecipeInstallationChange | RecipeRunChange | RunNodeChange:
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                componentsschemas_fleet_change_type_0 = NodeProfileChange.from_dict(data)



                return componentsschemas_fleet_change_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                componentsschemas_fleet_change_type_1 = RecipeInstallationChange.from_dict(data)



                return componentsschemas_fleet_change_type_1
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                componentsschemas_fleet_change_type_2 = InstallationNodeChange.from_dict(data)



                return componentsschemas_fleet_change_type_2
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                componentsschemas_fleet_change_type_3 = RecipeRunChange.from_dict(data)



                return componentsschemas_fleet_change_type_3
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                componentsschemas_fleet_change_type_4 = RunNodeChange.from_dict(data)



                return componentsschemas_fleet_change_type_4
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                componentsschemas_fleet_change_type_5 = JobChange.from_dict(data)



                return componentsschemas_fleet_change_type_5
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            if not isinstance(data, dict):
                raise TypeError()
            componentsschemas_fleet_change_type_6 = AgentOperationChange.from_dict(data)



            return componentsschemas_fleet_change_type_6

        change = _parse_change(d.pop("change"))


        event_cursor = d.pop("event_cursor")

        projection_refresh_required = d.pop("projection_refresh_required", UNSET)

        fleet_change_event = cls(
            change=change,
            event_cursor=event_cursor,
            projection_refresh_required=projection_refresh_required,
        )

        return fleet_change_event
