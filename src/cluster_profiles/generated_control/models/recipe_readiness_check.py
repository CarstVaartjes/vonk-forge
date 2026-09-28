from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.recipe_readiness_check_state import check_recipe_readiness_check_state
from ..models.recipe_readiness_check_state import RecipeReadinessCheckState
from ..types import UNSET, Unset
from typing import cast

if TYPE_CHECKING:
  from ..models.run_switch_reason import RunSwitchReason





T = TypeVar("T", bound="RecipeReadinessCheck")



@_attrs_define
class RecipeReadinessCheck:
    """
        Attributes:
            state (RecipeReadinessCheckState):
            reasons (list[RunSwitchReason] | Unset):
     """

    state: RecipeReadinessCheckState
    reasons: list[RunSwitchReason] | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.run_switch_reason import RunSwitchReason # noqa: PLC0415
        state: str = self.state

        reasons: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.reasons, Unset):
            reasons = []
            for reasons_item_data in self.reasons:
                reasons_item = reasons_item_data.to_dict()
                reasons.append(reasons_item)




        field_dict: dict[str, Any] = {}

        field_dict.update({
            "state": state,
        })
        if reasons is not UNSET:
            field_dict["reasons"] = reasons

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.run_switch_reason import RunSwitchReason # noqa: PLC0415
        d = dict(src_dict)
        state = check_recipe_readiness_check_state(d.pop("state"))




        _reasons = d.pop("reasons", UNSET)
        reasons: list[RunSwitchReason] | Unset = UNSET
        if _reasons is not UNSET:
            reasons = []
            for reasons_item_data in _reasons:
                reasons_item = RunSwitchReason.from_dict(reasons_item_data)



                reasons.append(reasons_item)


        recipe_readiness_check = cls(
            state=state,
            reasons=reasons,
        )

        return recipe_readiness_check
