from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Literal, Union, cast
from typing import Union

if TYPE_CHECKING:
  from ..models.job_change import JobChange
  from ..models.agent_operation_change import AgentOperationChange
  from ..models.run_node_change import RunNodeChange
  from ..models.node_profile_change import NodeProfileChange
  from ..models.installation_node_change import InstallationNodeChange
  from ..models.recipe_installation_change import RecipeInstallationChange
  from ..models.recipe_run_change import RecipeRunChange





T = TypeVar("T", bound="FleetChangeEvent")



@_attrs_define
class FleetChangeEvent:
    """
        Attributes:
            change (Union['AgentOperationChange', 'InstallationNodeChange', 'JobChange', 'NodeProfileChange',
                'RecipeInstallationChange', 'RecipeRunChange', 'RunNodeChange']):
            projection_refresh_required (Union[Unset, bool]):  Default: True.
            schema_version (Union[Literal[1], Unset]):  Default: 1.
     """

    change: Union['AgentOperationChange', 'InstallationNodeChange', 'JobChange', 'NodeProfileChange', 'RecipeInstallationChange', 'RecipeRunChange', 'RunNodeChange']
    projection_refresh_required: Union[Unset, bool] = True
    schema_version: Union[Literal[1], Unset] = 1





    def to_dict(self) -> dict[str, Any]:
        from ..models.job_change import JobChange
        from ..models.agent_operation_change import AgentOperationChange
        from ..models.run_node_change import RunNodeChange
        from ..models.node_profile_change import NodeProfileChange
        from ..models.installation_node_change import InstallationNodeChange
        from ..models.recipe_installation_change import RecipeInstallationChange
        from ..models.recipe_run_change import RecipeRunChange
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


        projection_refresh_required = self.projection_refresh_required

        schema_version = self.schema_version


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "change": change,
        })
        if projection_refresh_required is not UNSET:
            field_dict["projection_refresh_required"] = projection_refresh_required
        if schema_version is not UNSET:
            field_dict["schema_version"] = schema_version

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.job_change import JobChange
        from ..models.agent_operation_change import AgentOperationChange
        from ..models.run_node_change import RunNodeChange
        from ..models.node_profile_change import NodeProfileChange
        from ..models.installation_node_change import InstallationNodeChange
        from ..models.recipe_installation_change import RecipeInstallationChange
        from ..models.recipe_run_change import RecipeRunChange
        d = dict(src_dict)
        def _parse_change(data: object) -> Union['AgentOperationChange', 'InstallationNodeChange', 'JobChange', 'NodeProfileChange', 'RecipeInstallationChange', 'RecipeRunChange', 'RunNodeChange']:
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                componentsschemas_fleet_change_type_0 = NodeProfileChange.from_dict(data)



                return componentsschemas_fleet_change_type_0
            except: # noqa: E722
                pass
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                componentsschemas_fleet_change_type_1 = RecipeInstallationChange.from_dict(data)



                return componentsschemas_fleet_change_type_1
            except: # noqa: E722
                pass
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                componentsschemas_fleet_change_type_2 = InstallationNodeChange.from_dict(data)



                return componentsschemas_fleet_change_type_2
            except: # noqa: E722
                pass
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                componentsschemas_fleet_change_type_3 = RecipeRunChange.from_dict(data)



                return componentsschemas_fleet_change_type_3
            except: # noqa: E722
                pass
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                componentsschemas_fleet_change_type_4 = RunNodeChange.from_dict(data)



                return componentsschemas_fleet_change_type_4
            except: # noqa: E722
                pass
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                componentsschemas_fleet_change_type_5 = JobChange.from_dict(data)



                return componentsschemas_fleet_change_type_5
            except: # noqa: E722
                pass
            if not isinstance(data, dict):
                raise TypeError()
            componentsschemas_fleet_change_type_6 = AgentOperationChange.from_dict(data)



            return componentsschemas_fleet_change_type_6

        change = _parse_change(d.pop("change"))


        projection_refresh_required = d.pop("projection_refresh_required", UNSET)

        schema_version = cast(Union[Literal[1], Unset] , d.pop("schema_version", UNSET))
        if schema_version != 1 and not isinstance(schema_version, Unset):
            raise ValueError(f"schema_version must match const 1, got '{schema_version}'")

        fleet_change_event = cls(
            change=change,
            projection_refresh_required=projection_refresh_required,
            schema_version=schema_version,
        )

        return fleet_change_event
