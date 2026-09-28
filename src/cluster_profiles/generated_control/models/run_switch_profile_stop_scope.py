from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast

if TYPE_CHECKING:
  from ..models.spark_group import SparkGroup





T = TypeVar("T", bound="RunSwitchProfileStopScope")



@_attrs_define
class RunSwitchProfileStopScope:
    """ Reviewed profile-only cleanup of the reachable ranks in a lost group.

    The full accepted topology remains visible even though only its reachable
    subset is sent Stop work.  Missing ranks are explicit so a partial cleanup
    can never be presented as a successful full-group stop.

        Attributes:
            missing_node_ids (list[str]):
            original_group (SparkGroup): A complete, rank-labelled Spark group selected by the operator.
            target_node_ids (list[str]):
     """

    missing_node_ids: list[str]
    original_group: SparkGroup
    target_node_ids: list[str]





    def to_dict(self) -> dict[str, Any]:
        from ..models.spark_group import SparkGroup # noqa: PLC0415
        missing_node_ids = self.missing_node_ids



        original_group = self.original_group.to_dict()

        target_node_ids = self.target_node_ids




        field_dict: dict[str, Any] = {}

        field_dict.update({
            "missing_node_ids": missing_node_ids,
            "original_group": original_group,
            "target_node_ids": target_node_ids,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.spark_group import SparkGroup # noqa: PLC0415
        d = dict(src_dict)
        missing_node_ids = cast(list[str], d.pop("missing_node_ids"))


        original_group = SparkGroup.from_dict(d.pop("original_group"))




        target_node_ids = cast(list[str], d.pop("target_node_ids"))


        run_switch_profile_stop_scope = cls(
            missing_node_ids=missing_node_ids,
            original_group=original_group,
            target_node_ids=target_node_ids,
        )

        return run_switch_profile_stop_scope
