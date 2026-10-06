from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast
from typing import Literal, cast

if TYPE_CHECKING:
  from ..models.recipe_cache_removal_checkpoint import RecipeCacheRemovalCheckpoint
  from ..models.recipe_cache_removal_plan import RecipeCacheRemovalPlan





T = TypeVar("T", bound="RecipeCacheRemovalOwner")



@_attrs_define
class RecipeCacheRemovalOwner:
    """ One accepted exact plan plus its canonical durable effect checkpoint.

        Attributes:
            checkpoint (RecipeCacheRemovalCheckpoint): Durable resumable progress for a recipe image and model-cache
                removal.
            plan (RecipeCacheRemovalPlan): Immutable exact targets bound to the current request owner.
            schema_version (Literal[2]):
     """

    checkpoint: RecipeCacheRemovalCheckpoint
    plan: RecipeCacheRemovalPlan
    schema_version: Literal[2]





    def to_dict(self) -> dict[str, Any]:
        from ..models.recipe_cache_removal_checkpoint import RecipeCacheRemovalCheckpoint # noqa: PLC0415
        from ..models.recipe_cache_removal_plan import RecipeCacheRemovalPlan # noqa: PLC0415
        checkpoint = self.checkpoint.to_dict()

        plan = self.plan.to_dict()

        schema_version = self.schema_version


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "checkpoint": checkpoint,
            "plan": plan,
            "schema_version": schema_version,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.recipe_cache_removal_checkpoint import RecipeCacheRemovalCheckpoint # noqa: PLC0415
        from ..models.recipe_cache_removal_plan import RecipeCacheRemovalPlan # noqa: PLC0415
        d = dict(src_dict)
        checkpoint = RecipeCacheRemovalCheckpoint.from_dict(d.pop("checkpoint"))




        plan = RecipeCacheRemovalPlan.from_dict(d.pop("plan"))




        schema_version = cast(Literal[2] , d.pop("schema_version"))
        if schema_version != 2:
            raise ValueError(f"schema_version must match const 2, got '{schema_version}'")

        recipe_cache_removal_owner = cls(
            checkpoint=checkpoint,
            plan=plan,
            schema_version=schema_version,
        )

        return recipe_cache_removal_owner
