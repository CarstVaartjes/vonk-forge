from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.fleet_profile_definition_installation_policy import check_fleet_profile_definition_installation_policy
from ..models.fleet_profile_definition_installation_policy import FleetProfileDefinitionInstallationPolicy
from ..types import UNSET, Unset
from typing import cast

if TYPE_CHECKING:
  from ..models.fleet_profile_assignment_input import FleetProfileAssignmentInput
  from ..models.fleet_profile_definition_labels import FleetProfileDefinitionLabels





T = TypeVar("T", bound="FleetProfileDefinition")



@_attrs_define
class FleetProfileDefinition:
    """ Saved authoring intent, independent of execution and cache projections.

        Attributes:
            assignments (list[FleetProfileAssignmentInput] | Unset):
            description (str | Unset):  Default: ''.
            favorite (bool | Unset):  Default: False.
            installation_policy (FleetProfileDefinitionInstallationPolicy | Unset):  Default: 'keep-cached'.
            labels (FleetProfileDefinitionLabels | Unset):
            name (str | Unset):  Default: 'Default'.
     """

    assignments: list[FleetProfileAssignmentInput] | Unset = UNSET
    description: str | Unset = ''
    favorite: bool | Unset = False
    installation_policy: FleetProfileDefinitionInstallationPolicy | Unset = 'keep-cached'
    labels: FleetProfileDefinitionLabels | Unset = UNSET
    name: str | Unset = 'Default'





    def to_dict(self) -> dict[str, Any]:
        from ..models.fleet_profile_assignment_input import FleetProfileAssignmentInput # noqa: PLC0415
        from ..models.fleet_profile_definition_labels import FleetProfileDefinitionLabels # noqa: PLC0415
        assignments: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.assignments, Unset):
            assignments = []
            for assignments_item_data in self.assignments:
                assignments_item = assignments_item_data.to_dict()
                assignments.append(assignments_item)



        description = self.description

        favorite = self.favorite

        installation_policy: str | Unset = UNSET
        if not isinstance(self.installation_policy, Unset):
            installation_policy = self.installation_policy


        labels: dict[str, Any] | Unset = UNSET
        if not isinstance(self.labels, Unset):
            labels = self.labels.to_dict()

        name = self.name


        field_dict: dict[str, Any] = {}

        field_dict.update({
        })
        if assignments is not UNSET:
            field_dict["assignments"] = assignments
        if description is not UNSET:
            field_dict["description"] = description
        if favorite is not UNSET:
            field_dict["favorite"] = favorite
        if installation_policy is not UNSET:
            field_dict["installation_policy"] = installation_policy
        if labels is not UNSET:
            field_dict["labels"] = labels
        if name is not UNSET:
            field_dict["name"] = name

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.fleet_profile_assignment_input import FleetProfileAssignmentInput # noqa: PLC0415
        from ..models.fleet_profile_definition_labels import FleetProfileDefinitionLabels # noqa: PLC0415
        d = dict(src_dict)
        _assignments = d.pop("assignments", UNSET)
        assignments: list[FleetProfileAssignmentInput] | Unset = UNSET
        if _assignments is not UNSET:
            assignments = []
            for assignments_item_data in _assignments:
                assignments_item = FleetProfileAssignmentInput.from_dict(assignments_item_data)



                assignments.append(assignments_item)


        description = d.pop("description", UNSET)

        favorite = d.pop("favorite", UNSET)

        _installation_policy = d.pop("installation_policy", UNSET)
        installation_policy: FleetProfileDefinitionInstallationPolicy | Unset
        if isinstance(_installation_policy,  Unset):
            installation_policy = UNSET
        else:
            installation_policy = check_fleet_profile_definition_installation_policy(_installation_policy)




        _labels = d.pop("labels", UNSET)
        labels: FleetProfileDefinitionLabels | Unset
        if isinstance(_labels,  Unset):
            labels = UNSET
        else:
            labels = FleetProfileDefinitionLabels.from_dict(_labels)




        name = d.pop("name", UNSET)

        fleet_profile_definition = cls(
            assignments=assignments,
            description=description,
            favorite=favorite,
            installation_policy=installation_policy,
            labels=labels,
            name=name,
        )

        return fleet_profile_definition
