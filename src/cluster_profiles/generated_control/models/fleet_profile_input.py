from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.fleet_profile_input_installation_policy import check_fleet_profile_input_installation_policy
from ..models.fleet_profile_input_installation_policy import FleetProfileInputInstallationPolicy
from ..types import UNSET, Unset
from typing import cast
from typing import Union

if TYPE_CHECKING:
  from ..models.fleet_profile_scope import FleetProfileScope
  from ..models.fleet_profile_assignment_input import FleetProfileAssignmentInput
  from ..models.fleet_profile_input_labels import FleetProfileInputLabels





T = TypeVar("T", bound="FleetProfileInput")



@_attrs_define
class FleetProfileInput:
    """
        Attributes:
            name (str):
            scope (FleetProfileScope): The complete set of Sparks reconciled by a profile.

                Scope is deliberately independent from assignments.  A member with no
                assignment is an intentional idle outcome when the profile is applied.
            assignments (Union[Unset, list['FleetProfileAssignmentInput']]):
            description (Union[Unset, str]):  Default: ''.
            favorite (Union[Unset, bool]):  Default: False.
            installation_policy (Union[Unset, FleetProfileInputInstallationPolicy]):  Default: 'keep-cached'.
            labels (Union[Unset, FleetProfileInputLabels]):
     """

    name: str
    scope: 'FleetProfileScope'
    assignments: Union[Unset, list['FleetProfileAssignmentInput']] = UNSET
    description: Union[Unset, str] = ''
    favorite: Union[Unset, bool] = False
    installation_policy: Union[Unset, FleetProfileInputInstallationPolicy] = 'keep-cached'
    labels: Union[Unset, 'FleetProfileInputLabels'] = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.fleet_profile_scope import FleetProfileScope
        from ..models.fleet_profile_assignment_input import FleetProfileAssignmentInput
        from ..models.fleet_profile_input_labels import FleetProfileInputLabels
        name = self.name

        scope = self.scope.to_dict()

        assignments: Union[Unset, list[dict[str, Any]]] = UNSET
        if not isinstance(self.assignments, Unset):
            assignments = []
            for assignments_item_data in self.assignments:
                assignments_item = assignments_item_data.to_dict()
                assignments.append(assignments_item)



        description = self.description

        favorite = self.favorite

        installation_policy: Union[Unset, str] = UNSET
        if not isinstance(self.installation_policy, Unset):
            installation_policy = self.installation_policy


        labels: Union[Unset, dict[str, Any]] = UNSET
        if not isinstance(self.labels, Unset):
            labels = self.labels.to_dict()


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "name": name,
            "scope": scope,
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

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.fleet_profile_scope import FleetProfileScope
        from ..models.fleet_profile_assignment_input import FleetProfileAssignmentInput
        from ..models.fleet_profile_input_labels import FleetProfileInputLabels
        d = dict(src_dict)
        name = d.pop("name")

        scope = FleetProfileScope.from_dict(d.pop("scope"))




        assignments = []
        _assignments = d.pop("assignments", UNSET)
        for assignments_item_data in (_assignments or []):
            assignments_item = FleetProfileAssignmentInput.from_dict(assignments_item_data)



            assignments.append(assignments_item)


        description = d.pop("description", UNSET)

        favorite = d.pop("favorite", UNSET)

        _installation_policy = d.pop("installation_policy", UNSET)
        installation_policy: Union[Unset, FleetProfileInputInstallationPolicy]
        if isinstance(_installation_policy,  Unset):
            installation_policy = UNSET
        else:
            installation_policy = check_fleet_profile_input_installation_policy(_installation_policy)




        _labels = d.pop("labels", UNSET)
        labels: Union[Unset, FleetProfileInputLabels]
        if isinstance(_labels,  Unset):
            labels = UNSET
        else:
            labels = FleetProfileInputLabels.from_dict(_labels)




        fleet_profile_input = cls(
            name=name,
            scope=scope,
            assignments=assignments,
            description=description,
            favorite=favorite,
            installation_policy=installation_policy,
            labels=labels,
        )

        return fleet_profile_input
