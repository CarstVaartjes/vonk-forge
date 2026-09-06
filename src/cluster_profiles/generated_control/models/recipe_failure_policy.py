from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.recipe_failure_policy_rank_loss import check_recipe_failure_policy_rank_loss
from ..models.recipe_failure_policy_rank_loss import RecipeFailurePolicyRankLoss
from ..models.recipe_failure_policy_recovery import check_recipe_failure_policy_recovery
from ..models.recipe_failure_policy_recovery import RecipeFailurePolicyRecovery
from typing import cast






T = TypeVar("T", bound="RecipeFailurePolicy")



@_attrs_define
class RecipeFailurePolicy:
    """
        Attributes:
            rank_loss (RecipeFailurePolicyRankLoss):
            recovery (RecipeFailurePolicyRecovery):
     """

    rank_loss: RecipeFailurePolicyRankLoss
    recovery: RecipeFailurePolicyRecovery





    def to_dict(self) -> dict[str, Any]:
        rank_loss: str = self.rank_loss

        recovery: str = self.recovery


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "rank_loss": rank_loss,
            "recovery": recovery,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        rank_loss = check_recipe_failure_policy_rank_loss(d.pop("rank_loss"))




        recovery = check_recipe_failure_policy_recovery(d.pop("recovery"))




        recipe_failure_policy = cls(
            rank_loss=rank_loss,
            recovery=recovery,
        )

        return recipe_failure_policy
