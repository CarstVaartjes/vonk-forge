from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast

if TYPE_CHECKING:
  from ..models.recipe_build_cleanup_request import RecipeBuildCleanupRequest





T = TypeVar("T", bound="BuildCleanupPhaseOperation")



@_attrs_define
class BuildCleanupPhaseOperation:
    """
        Attributes:
            node_id (str):
            operation_id (str):
            payload (RecipeBuildCleanupRequest): Stop only the transient service belonging to one retained build attempt.
     """

    node_id: str
    operation_id: str
    payload: RecipeBuildCleanupRequest





    def to_dict(self) -> dict[str, Any]:
        from ..models.recipe_build_cleanup_request import RecipeBuildCleanupRequest # noqa: PLC0415
        node_id = self.node_id

        operation_id = self.operation_id

        payload = self.payload.to_dict()


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "node_id": node_id,
            "operation_id": operation_id,
            "payload": payload,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.recipe_build_cleanup_request import RecipeBuildCleanupRequest # noqa: PLC0415
        d = dict(src_dict)
        node_id = d.pop("node_id")

        operation_id = d.pop("operation_id")

        payload = RecipeBuildCleanupRequest.from_dict(d.pop("payload"))




        build_cleanup_phase_operation = cls(
            node_id=node_id,
            operation_id=operation_id,
            payload=payload,
        )

        return build_cleanup_phase_operation
