from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.fleet_profile_view_installation_policy import check_fleet_profile_view_installation_policy
from ..models.fleet_profile_view_installation_policy import FleetProfileViewInstallationPolicy
from ..types import UNSET, Unset
from typing import cast
from typing import Literal, cast
import datetime

if TYPE_CHECKING:
  from ..models.fleet_profile_assignment_view import FleetProfileAssignmentView
  from ..models.fleet_profile_definition import FleetProfileDefinition
  from ..models.fleet_profile_view_cache_summary import FleetProfileViewCacheSummary
  from ..models.fleet_profile_view_fleet_item import FleetProfileViewFleetItem
  from ..models.fleet_profile_view_labels import FleetProfileViewLabels





T = TypeVar("T", bound="FleetProfileView")



@_attrs_define
class FleetProfileView:
    """
        Attributes:
            assignments (list[FleetProfileAssignmentView]):
            created_at (datetime.datetime):
            created_by (str):
            definition (FleetProfileDefinition): Saved authoring intent, independent of execution and cache projections.
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
            cache_summary (FleetProfileViewCacheSummary | Unset):
            fleet (list[FleetProfileViewFleetItem] | Unset):
            loaded_revision (int | None | Unset):
            next_actions (list[str] | Unset):
            schema_version (Literal[2] | Unset):  Default: 2.
            status (str | Unset):  Default: 'draft'.
            warnings (list[str] | Unset):
     """

    assignments: list[FleetProfileAssignmentView]
    created_at: datetime.datetime
    created_by: str
    definition: FleetProfileDefinition
    description: str
    favorite: bool
    id: str
    installation_policy: FleetProfileViewInstallationPolicy
    labels: FleetProfileViewLabels
    name: str
    number: int
    profile_digest: str
    revision: int
    updated_at: datetime.datetime
    cache_summary: FleetProfileViewCacheSummary | Unset = UNSET
    fleet: list[FleetProfileViewFleetItem] | Unset = UNSET
    loaded_revision: int | None | Unset = UNSET
    next_actions: list[str] | Unset = UNSET
    schema_version: Literal[2] | Unset = 2
    status: str | Unset = 'draft'
    warnings: list[str] | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.fleet_profile_assignment_view import FleetProfileAssignmentView # noqa: PLC0415
        from ..models.fleet_profile_definition import FleetProfileDefinition # noqa: PLC0415
        from ..models.fleet_profile_view_cache_summary import FleetProfileViewCacheSummary # noqa: PLC0415
        from ..models.fleet_profile_view_fleet_item import FleetProfileViewFleetItem # noqa: PLC0415
        from ..models.fleet_profile_view_labels import FleetProfileViewLabels # noqa: PLC0415
        assignments = []
        for assignments_item_data in self.assignments:
            assignments_item = assignments_item_data.to_dict()
            assignments.append(assignments_item)



        created_at = self.created_at.isoformat()

        created_by = self.created_by

        definition = self.definition.to_dict()

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

        cache_summary: dict[str, Any] | Unset = UNSET
        if not isinstance(self.cache_summary, Unset):
            cache_summary = self.cache_summary.to_dict()

        fleet: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.fleet, Unset):
            fleet = []
            for fleet_item_data in self.fleet:
                fleet_item = fleet_item_data.to_dict()
                fleet.append(fleet_item)



        loaded_revision: int | None | Unset
        if isinstance(self.loaded_revision, Unset):
            loaded_revision = UNSET
        else:
            loaded_revision = self.loaded_revision

        next_actions: list[str] | Unset = UNSET
        if not isinstance(self.next_actions, Unset):
            next_actions = self.next_actions



        schema_version = self.schema_version

        status = self.status

        warnings: list[str] | Unset = UNSET
        if not isinstance(self.warnings, Unset):
            warnings = self.warnings




        field_dict: dict[str, Any] = {}

        field_dict.update({
            "assignments": assignments,
            "created_at": created_at,
            "created_by": created_by,
            "definition": definition,
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
        from ..models.fleet_profile_assignment_view import FleetProfileAssignmentView # noqa: PLC0415
        from ..models.fleet_profile_definition import FleetProfileDefinition # noqa: PLC0415
        from ..models.fleet_profile_view_cache_summary import FleetProfileViewCacheSummary # noqa: PLC0415
        from ..models.fleet_profile_view_fleet_item import FleetProfileViewFleetItem # noqa: PLC0415
        from ..models.fleet_profile_view_labels import FleetProfileViewLabels # noqa: PLC0415
        d = dict(src_dict)
        assignments = []
        _assignments = d.pop("assignments")
        for assignments_item_data in (_assignments):
            assignments_item = FleetProfileAssignmentView.from_dict(assignments_item_data)



            assignments.append(assignments_item)


        created_at = datetime.datetime.fromisoformat(d.pop("created_at"))




        created_by = d.pop("created_by")

        definition = FleetProfileDefinition.from_dict(d.pop("definition"))




        description = d.pop("description")

        favorite = d.pop("favorite")

        id = d.pop("id")

        installation_policy = check_fleet_profile_view_installation_policy(d.pop("installation_policy"))




        labels = FleetProfileViewLabels.from_dict(d.pop("labels"))




        name = d.pop("name")

        number = d.pop("number")

        profile_digest = d.pop("profile_digest")

        revision = d.pop("revision")

        updated_at = datetime.datetime.fromisoformat(d.pop("updated_at"))




        _cache_summary = d.pop("cache_summary", UNSET)
        cache_summary: FleetProfileViewCacheSummary | Unset
        if isinstance(_cache_summary,  Unset):
            cache_summary = UNSET
        else:
            cache_summary = FleetProfileViewCacheSummary.from_dict(_cache_summary)




        _fleet = d.pop("fleet", UNSET)
        fleet: list[FleetProfileViewFleetItem] | Unset = UNSET
        if _fleet is not UNSET:
            fleet = []
            for fleet_item_data in _fleet:
                fleet_item = FleetProfileViewFleetItem.from_dict(fleet_item_data)



                fleet.append(fleet_item)


        def _parse_loaded_revision(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        loaded_revision = _parse_loaded_revision(d.pop("loaded_revision", UNSET))


        next_actions = cast(list[str], d.pop("next_actions", UNSET))


        schema_version = cast(Literal[2] | Unset , d.pop("schema_version", UNSET))
        if schema_version != 2 and not isinstance(schema_version, Unset):
            raise ValueError(f"schema_version must match const 2, got '{schema_version}'")

        status = d.pop("status", UNSET)

        warnings = cast(list[str], d.pop("warnings", UNSET))


        fleet_profile_view = cls(
            assignments=assignments,
            created_at=created_at,
            created_by=created_by,
            definition=definition,
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
