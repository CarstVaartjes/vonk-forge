from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.fleet_profile_capture_input_installation_policy import check_fleet_profile_capture_input_installation_policy
from ..models.fleet_profile_capture_input_installation_policy import FleetProfileCaptureInputInstallationPolicy
from ..types import UNSET, Unset
from typing import cast
from typing import Union

if TYPE_CHECKING:
  from ..models.fleet_profile_capture_input_labels import FleetProfileCaptureInputLabels





T = TypeVar("T", bound="FleetProfileCaptureInput")



@_attrs_define
class FleetProfileCaptureInput:
    """
        Attributes:
            name (str):
            description (Union[Unset, str]):  Default: 'Captured current Fleet setup'.
            favorite (Union[Unset, bool]):  Default: False.
            installation_policy (Union[Unset, FleetProfileCaptureInputInstallationPolicy]):  Default: 'keep-cached'.
            labels (Union[Unset, FleetProfileCaptureInputLabels]):
     """

    name: str
    description: Union[Unset, str] = 'Captured current Fleet setup'
    favorite: Union[Unset, bool] = False
    installation_policy: Union[Unset, FleetProfileCaptureInputInstallationPolicy] = 'keep-cached'
    labels: Union[Unset, 'FleetProfileCaptureInputLabels'] = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.fleet_profile_capture_input_labels import FleetProfileCaptureInputLabels
        name = self.name

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
        })
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
        from ..models.fleet_profile_capture_input_labels import FleetProfileCaptureInputLabels
        d = dict(src_dict)
        name = d.pop("name")

        description = d.pop("description", UNSET)

        favorite = d.pop("favorite", UNSET)

        _installation_policy = d.pop("installation_policy", UNSET)
        installation_policy: Union[Unset, FleetProfileCaptureInputInstallationPolicy]
        if isinstance(_installation_policy,  Unset):
            installation_policy = UNSET
        else:
            installation_policy = check_fleet_profile_capture_input_installation_policy(_installation_policy)




        _labels = d.pop("labels", UNSET)
        labels: Union[Unset, FleetProfileCaptureInputLabels]
        if isinstance(_labels,  Unset):
            labels = UNSET
        else:
            labels = FleetProfileCaptureInputLabels.from_dict(_labels)




        fleet_profile_capture_input = cls(
            name=name,
            description=description,
            favorite=favorite,
            installation_policy=installation_policy,
            labels=labels,
        )

        return fleet_profile_capture_input
