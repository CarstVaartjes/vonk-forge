from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.fleet_profile_application_effect_kind import check_fleet_profile_application_effect_kind
from ..models.fleet_profile_application_effect_kind import FleetProfileApplicationEffectKind
from ..models.fleet_profile_application_effect_outcome import check_fleet_profile_application_effect_outcome
from ..models.fleet_profile_application_effect_outcome import FleetProfileApplicationEffectOutcome
from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Union






T = TypeVar("T", bound="FleetProfileApplicationEffect")



@_attrs_define
class FleetProfileApplicationEffect:
    """ One exact profile or child effect in the cancellation receipt.

        Attributes:
            effect_id (str):
            kind (FleetProfileApplicationEffectKind):
            label (str):
            outcome (FleetProfileApplicationEffectOutcome):
            operation_id (Union[None, Unset, str]):
     """

    effect_id: str
    kind: FleetProfileApplicationEffectKind
    label: str
    outcome: FleetProfileApplicationEffectOutcome
    operation_id: Union[None, Unset, str] = UNSET





    def to_dict(self) -> dict[str, Any]:
        effect_id = self.effect_id

        kind: str = self.kind

        label = self.label

        outcome: str = self.outcome

        operation_id: Union[None, Unset, str]
        if isinstance(self.operation_id, Unset):
            operation_id = UNSET
        else:
            operation_id = self.operation_id


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "effect_id": effect_id,
            "kind": kind,
            "label": label,
            "outcome": outcome,
        })
        if operation_id is not UNSET:
            field_dict["operation_id"] = operation_id

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        effect_id = d.pop("effect_id")

        kind = check_fleet_profile_application_effect_kind(d.pop("kind"))




        label = d.pop("label")

        outcome = check_fleet_profile_application_effect_outcome(d.pop("outcome"))




        def _parse_operation_id(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        operation_id = _parse_operation_id(d.pop("operation_id", UNSET))


        fleet_profile_application_effect = cls(
            effect_id=effect_id,
            kind=kind,
            label=label,
            outcome=outcome,
            operation_id=operation_id,
        )

        return fleet_profile_application_effect
