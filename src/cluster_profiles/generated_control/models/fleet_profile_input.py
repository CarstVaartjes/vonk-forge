from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.fleet_profile_input_installation_policy import check_fleet_profile_input_installation_policy
from ..models.fleet_profile_input_installation_policy import FleetProfileInputInstallationPolicy
from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Union

if TYPE_CHECKING:
  from ..models.fleet_profile_assignment_input import FleetProfileAssignmentInput
  from ..models.fleet_profile_input_labels import FleetProfileInputLabels





T = TypeVar("T", bound="FleetProfileInput")



@_attrs_define
class FleetProfileInput:
    """
        Attributes:
            assignments (Union[Unset, list['FleetProfileAssignmentInput']]):
            description (Union[Unset, str]):  Default: ''.
            expected_revision (Union[None, Unset, int]):
            favorite (Union[Unset, bool]):  Default: False.
            installation_policy (Union[Unset, FleetProfileInputInstallationPolicy]):  Default: 'keep-cached'.
            labels (Union[Unset, FleetProfileInputLabels]):
            name (Union[Unset, str]):  Default: 'Default'.
     """

    assignments: Union[Unset, list['FleetProfileAssignmentInput']] = UNSET
    description: Union[Unset, str] = ''
    expected_revision: Union[None, Unset, int] = UNSET
    favorite: Union[Unset, bool] = False
    installation_policy: Union[Unset, FleetProfileInputInstallationPolicy] = 'keep-cached'
    labels: Union[Unset, 'FleetProfileInputLabels'] = UNSET
    name: Union[Unset, str] = 'Default'





    def to_dict(self) -> dict[str, Any]:
        from ..models.fleet_profile_assignment_input import FleetProfileAssignmentInput
        from ..models.fleet_profile_input_labels import FleetProfileInputLabels
        assignments: Union[Unset, list[dict[str, Any]]] = UNSET
        if not isinstance(self.assignments, Unset):
            assignments = []
            for assignments_item_data in self.assignments:
                assignments_item = assignments_item_data.to_dict()
                assignments.append(assignments_item)



        description = self.description

        expected_revision: Union[None, Unset, int]
        if isinstance(self.expected_revision, Unset):
            expected_revision = UNSET
        else:
            expected_revision = self.expected_revision

        favorite = self.favorite

        installation_policy: Union[Unset, str] = UNSET
        if not isinstance(self.installation_policy, Unset):
            installation_policy = self.installation_policy


        labels: Union[Unset, dict[str, Any]] = UNSET
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
        if expected_revision is not UNSET:
            field_dict["expected_revision"] = expected_revision
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
        from ..models.fleet_profile_assignment_input import FleetProfileAssignmentInput
        from ..models.fleet_profile_input_labels import FleetProfileInputLabels
        d = dict(src_dict)
        assignments = []
        _assignments = d.pop("assignments", UNSET)
        for assignments_item_data in (_assignments or []):
            assignments_item = FleetProfileAssignmentInput.from_dict(assignments_item_data)



            assignments.append(assignments_item)


        description = d.pop("description", UNSET)

        def _parse_expected_revision(data: object) -> Union[None, Unset, int]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, int], data)

        expected_revision = _parse_expected_revision(d.pop("expected_revision", UNSET))


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




        name = d.pop("name", UNSET)

        fleet_profile_input = cls(
            assignments=assignments,
            description=description,
            expected_revision=expected_revision,
            favorite=favorite,
            installation_policy=installation_policy,
            labels=labels,
            name=name,
        )

        return fleet_profile_input
