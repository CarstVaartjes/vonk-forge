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
from typing import Literal, Union, cast
import datetime

if TYPE_CHECKING:
  from ..models.fleet_profile_view_labels import FleetProfileViewLabels
  from ..models.fleet_profile_assignment import FleetProfileAssignment





T = TypeVar("T", bound="FleetProfileView")



@_attrs_define
class FleetProfileView:
    """
        Attributes:
            assignments (list['FleetProfileAssignment']):
            created_at (datetime.datetime):
            created_by (str):
            description (str):
            favorite (bool):
            id (str):
            installation_policy (FleetProfileViewInstallationPolicy):
            labels (FleetProfileViewLabels):
            name (str):
            profile_digest (str):
            updated_at (datetime.datetime):
            schema_version (Union[Literal[1], Unset]):  Default: 1.
     """

    assignments: list['FleetProfileAssignment']
    created_at: datetime.datetime
    created_by: str
    description: str
    favorite: bool
    id: str
    installation_policy: FleetProfileViewInstallationPolicy
    labels: 'FleetProfileViewLabels'
    name: str
    profile_digest: str
    updated_at: datetime.datetime
    schema_version: Union[Literal[1], Unset] = 1





    def to_dict(self) -> dict[str, Any]:
        from ..models.fleet_profile_view_labels import FleetProfileViewLabels
        from ..models.fleet_profile_assignment import FleetProfileAssignment
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

        profile_digest = self.profile_digest

        updated_at = self.updated_at.isoformat()

        schema_version = self.schema_version


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
            "profile_digest": profile_digest,
            "updated_at": updated_at,
        })
        if schema_version is not UNSET:
            field_dict["schema_version"] = schema_version

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.fleet_profile_view_labels import FleetProfileViewLabels
        from ..models.fleet_profile_assignment import FleetProfileAssignment
        d = dict(src_dict)
        assignments = []
        _assignments = d.pop("assignments")
        for assignments_item_data in (_assignments):
            assignments_item = FleetProfileAssignment.from_dict(assignments_item_data)



            assignments.append(assignments_item)


        created_at = isoparse(d.pop("created_at"))




        created_by = d.pop("created_by")

        description = d.pop("description")

        favorite = d.pop("favorite")

        id = d.pop("id")

        installation_policy = check_fleet_profile_view_installation_policy(d.pop("installation_policy"))




        labels = FleetProfileViewLabels.from_dict(d.pop("labels"))




        name = d.pop("name")

        profile_digest = d.pop("profile_digest")

        updated_at = isoparse(d.pop("updated_at"))




        schema_version = cast(Union[Literal[1], Unset] , d.pop("schema_version", UNSET))
        if schema_version != 1 and not isinstance(schema_version, Unset):
            raise ValueError(f"schema_version must match const 1, got '{schema_version}'")

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
            profile_digest=profile_digest,
            updated_at=updated_at,
            schema_version=schema_version,
        )

        return fleet_profile_view
