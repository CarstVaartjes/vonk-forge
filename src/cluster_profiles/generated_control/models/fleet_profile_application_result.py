from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="FleetProfileApplicationResult")



@_attrs_define
class FleetProfileApplicationResult:
    """ Terminal result for one profile application.

        Attributes:
            changed (bool):
            completed_steps (int):
     """

    changed: bool
    completed_steps: int





    def to_dict(self) -> dict[str, Any]:
        changed = self.changed

        completed_steps = self.completed_steps


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "changed": changed,
            "completed_steps": completed_steps,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        changed = d.pop("changed")

        completed_steps = d.pop("completed_steps")

        fleet_profile_application_result = cls(
            changed=changed,
            completed_steps=completed_steps,
        )

        return fleet_profile_application_result
