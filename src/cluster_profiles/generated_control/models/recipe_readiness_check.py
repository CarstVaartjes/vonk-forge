from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.recipe_readiness_check_state import check_recipe_readiness_check_state
from ..models.recipe_readiness_check_state import RecipeReadinessCheckState
from ..types import UNSET, Unset
from typing import cast
from typing import Union

if TYPE_CHECKING:
  from ..models.run_switch_reason import RunSwitchReason





T = TypeVar("T", bound="RecipeReadinessCheck")



@_attrs_define
class RecipeReadinessCheck:
    """
        Attributes:
            state (RecipeReadinessCheckState):
            reasons (Union[Unset, list['RunSwitchReason']]):
     """

    state: RecipeReadinessCheckState
    reasons: Union[Unset, list['RunSwitchReason']] = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.run_switch_reason import RunSwitchReason
        state: str = self.state

        reasons: Union[Unset, list[dict[str, Any]]] = UNSET
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
        from ..models.run_switch_reason import RunSwitchReason
        d = dict(src_dict)
        state = check_recipe_readiness_check_state(d.pop("state"))




        reasons = []
        _reasons = d.pop("reasons", UNSET)
        for reasons_item_data in (_reasons or []):
            reasons_item = RunSwitchReason.from_dict(reasons_item_data)



            reasons.append(reasons_item)


        recipe_readiness_check = cls(
            state=state,
            reasons=reasons,
        )

        return recipe_readiness_check
