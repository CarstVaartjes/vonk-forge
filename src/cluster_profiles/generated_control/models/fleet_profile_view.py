from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.fleet_profile_view_installation_policy import check_fleet_profile_view_installation_policy
from ..models.fleet_profile_view_installation_policy import FleetProfileViewInstallationPolicy
from ..types import UNSET, Unset
from dateutil.parser import isoparse
from typing import cast
from typing import cast, Union
from typing import Literal, Union, cast
from typing import Union
import datetime

if TYPE_CHECKING:
  from ..models.fleet_profile_view_fleet_item import FleetProfileViewFleetItem
  from ..models.fleet_profile_assignment_view import FleetProfileAssignmentView
  from ..models.fleet_profile_view_cache_summary import FleetProfileViewCacheSummary
  from ..models.fleet_profile_view_labels import FleetProfileViewLabels





T = TypeVar("T", bound="FleetProfileView")



@_attrs_define
class FleetProfileView:
    """
        Attributes:
            assignments (list['FleetProfileAssignmentView']):
            created_at (datetime.datetime):
            created_by (str):
            description (str):
            favorite (bool):
            id (str):
            installation_policy (FleetProfileViewInstallationPolicy):
            labels (FleetProfileViewLabels):
            name (str):
            number (int):
            profile_digest (str):
            revision (int):
            updated_at (datetime.datetime):
            cache_summary (Union[Unset, FleetProfileViewCacheSummary]):
            fleet (Union[Unset, list['FleetProfileViewFleetItem']]):
            loaded_revision (Union[None, Unset, int]):
            next_actions (Union[Unset, list[str]]):
            schema_version (Union[Literal[2], Unset]):  Default: 2.
            status (Union[Unset, str]):  Default: 'draft'.
            warnings (Union[Unset, list[str]]):
     """

    assignments: list['FleetProfileAssignmentView']
    created_at: datetime.datetime
    created_by: str
    description: str
    favorite: bool
    id: str
    installation_policy: FleetProfileViewInstallationPolicy
    labels: 'FleetProfileViewLabels'
    name: str
    number: int
    profile_digest: str
    revision: int
    updated_at: datetime.datetime
    cache_summary: Union[Unset, 'FleetProfileViewCacheSummary'] = UNSET
    fleet: Union[Unset, list['FleetProfileViewFleetItem']] = UNSET
    loaded_revision: Union[None, Unset, int] = UNSET
    next_actions: Union[Unset, list[str]] = UNSET
    schema_version: Union[Literal[2], Unset] = 2
    status: Union[Unset, str] = 'draft'
    warnings: Union[Unset, list[str]] = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.fleet_profile_view_fleet_item import FleetProfileViewFleetItem
        from ..models.fleet_profile_assignment_view import FleetProfileAssignmentView
        from ..models.fleet_profile_view_cache_summary import FleetProfileViewCacheSummary
        from ..models.fleet_profile_view_labels import FleetProfileViewLabels
        assignments = []
        for assignments_item_data in self.assignments:
            assignments_item = assignments_item_data.to_dict()
            assignments.append(assignments_item)



        created_at = self.created_at.isoformat()

        created_by = self.created_by

        description = self.description

        favorite = self.favorite

        id = self.id

        installation_policy: str = self.installation_policy

        labels = self.labels.to_dict()

        name = self.name

        number = self.number

        profile_digest = self.profile_digest

        revision = self.revision

        updated_at = self.updated_at.isoformat()

        cache_summary: Union[Unset, dict[str, Any]] = UNSET
        if not isinstance(self.cache_summary, Unset):
            cache_summary = self.cache_summary.to_dict()

        fleet: Union[Unset, list[dict[str, Any]]] = UNSET
        if not isinstance(self.fleet, Unset):
            fleet = []
            for fleet_item_data in self.fleet:
                fleet_item = fleet_item_data.to_dict()
                fleet.append(fleet_item)



        loaded_revision: Union[None, Unset, int]
        if isinstance(self.loaded_revision, Unset):
            loaded_revision = UNSET
        else:
            loaded_revision = self.loaded_revision

        next_actions: Union[Unset, list[str]] = UNSET
        if not isinstance(self.next_actions, Unset):
            next_actions = self.next_actions



        schema_version = self.schema_version

        status = self.status

        warnings: Union[Unset, list[str]] = UNSET
        if not isinstance(self.warnings, Unset):
            warnings = self.warnings




        field_dict: dict[str, Any] = {}

        field_dict.update({
            "assignments": assignments,
            "created_at": created_at,
            "created_by": created_by,
            "description": description,
            "favorite": favorite,
            "id": id,
            "installation_policy": installation_policy,
            "labels": labels,
            "name": name,
            "number": number,
            "profile_digest": profile_digest,
            "revision": revision,
            "updated_at": updated_at,
        })
        if cache_summary is not UNSET:
            field_dict["cache_summary"] = cache_summary
        if fleet is not UNSET:
            field_dict["fleet"] = fleet
        if loaded_revision is not UNSET:
            field_dict["loaded_revision"] = loaded_revision
        if next_actions is not UNSET:
            field_dict["next_actions"] = next_actions
        if schema_version is not UNSET:
            field_dict["schema_version"] = schema_version
        if status is not UNSET:
            field_dict["status"] = status
        if warnings is not UNSET:
            field_dict["warnings"] = warnings

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.fleet_profile_view_fleet_item import FleetProfileViewFleetItem
        from ..models.fleet_profile_assignment_view import FleetProfileAssignmentView
        from ..models.fleet_profile_view_cache_summary import FleetProfileViewCacheSummary
        from ..models.fleet_profile_view_labels import FleetProfileViewLabels
        d = dict(src_dict)
        assignments = []
        _assignments = d.pop("assignments")
        for assignments_item_data in (_assignments):
            assignments_item = FleetProfileAssignmentView.from_dict(assignments_item_data)



            assignments.append(assignments_item)


        created_at = isoparse(d.pop("created_at"))




        created_by = d.pop("created_by")

        description = d.pop("description")

        favorite = d.pop("favorite")

        id = d.pop("id")

        installation_policy = check_fleet_profile_view_installation_policy(d.pop("installation_policy"))




        labels = FleetProfileViewLabels.from_dict(d.pop("labels"))




        name = d.pop("name")

        number = d.pop("number")

        profile_digest = d.pop("profile_digest")

        revision = d.pop("revision")

        updated_at = isoparse(d.pop("updated_at"))




        _cache_summary = d.pop("cache_summary", UNSET)
        cache_summary: Union[Unset, FleetProfileViewCacheSummary]
        if isinstance(_cache_summary,  Unset):
            cache_summary = UNSET
        else:
            cache_summary = FleetProfileViewCacheSummary.from_dict(_cache_summary)




        fleet = []
        _fleet = d.pop("fleet", UNSET)
        for fleet_item_data in (_fleet or []):
            fleet_item = FleetProfileViewFleetItem.from_dict(fleet_item_data)



            fleet.append(fleet_item)


        def _parse_loaded_revision(data: object) -> Union[None, Unset, int]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, int], data)

        loaded_revision = _parse_loaded_revision(d.pop("loaded_revision", UNSET))


        next_actions = cast(list[str], d.pop("next_actions", UNSET))


        schema_version = cast(Union[Literal[2], Unset] , d.pop("schema_version", UNSET))
        if schema_version != 2 and not isinstance(schema_version, Unset):
            raise ValueError(f"schema_version must match const 2, got '{schema_version}'")

        status = d.pop("status", UNSET)

        warnings = cast(list[str], d.pop("warnings", UNSET))


        fleet_profile_view = cls(
            assignments=assignments,
            created_at=created_at,
            created_by=created_by,
            description=description,
            favorite=favorite,
            id=id,
            installation_policy=installation_policy,
            labels=labels,
            name=name,
            number=number,
            profile_digest=profile_digest,
            revision=revision,
            updated_at=updated_at,
            cache_summary=cache_summary,
            fleet=fleet,
            loaded_revision=loaded_revision,
            next_actions=next_actions,
            schema_version=schema_version,
            status=status,
            warnings=warnings,
        )

        return fleet_profile_view
