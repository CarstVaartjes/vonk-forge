from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast

if TYPE_CHECKING:
  from ..models.recipe_stop_payload import RecipeStopPayload





T = TypeVar("T", bound="ProfileJobRunStopPhaseItem")



@_attrs_define
class ProfileJobRunStopPhaseItem:
    """
        Attributes:
            node_id (str):
            operation_id (str):
            payload (RecipeStopPayload): Exact cleanup authority, independent of historical launch-plan readability.

                The Controller binds these identities and timeout into the signed helper
                grant; the helper reconciles only the matching runtime generation.
     """

    node_id: str
    operation_id: str
    payload: RecipeStopPayload
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)





    def to_dict(self) -> dict[str, Any]:
        from ..models.recipe_stop_payload import RecipeStopPayload # noqa: PLC0415
        node_id = self.node_id

        operation_id = self.operation_id

        payload = self.payload.to_dict()


        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({
            "node_id": node_id,
            "operation_id": operation_id,
            "payload": payload,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.recipe_stop_payload import RecipeStopPayload # noqa: PLC0415
        d = dict(src_dict)
        node_id = d.pop("node_id")

        operation_id = d.pop("operation_id")

        payload = RecipeStopPayload.from_dict(d.pop("payload"))




        profile_job_run_stop_phase_item = cls(
            node_id=node_id,
            operation_id=operation_id,
            payload=payload,
        )


        profile_job_run_stop_phase_item.additional_properties = d
        return profile_job_run_stop_phase_item

    @property
    def additional_keys(self) -> list[str]:
        return list(self.additional_properties.keys())

    def __getitem__(self, key: str) -> Any:
        return self.additional_properties[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self.additional_properties[key] = value

    def __delitem__(self, key: str) -> None:
        del self.additional_properties[key]

    def __contains__(self, key: str) -> bool:
        return key in self.additional_properties
