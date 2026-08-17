from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.recipe_memory_requirements_kind import check_recipe_memory_requirements_kind
from ..models.recipe_memory_requirements_kind import RecipeMemoryRequirementsKind
from typing import cast






T = TypeVar("T", bound="RecipeMemoryRequirements")



@_attrs_define
class RecipeMemoryRequirements:
    """
        Attributes:
            kind (RecipeMemoryRequirementsKind):
            runtime_growth_bytes (int):
            startup_peak_bytes (int):
            steady_state_bytes (int):
            system_reserve_bytes (int):
     """

    kind: RecipeMemoryRequirementsKind
    runtime_growth_bytes: int
    startup_peak_bytes: int
    steady_state_bytes: int
    system_reserve_bytes: int





    def to_dict(self) -> dict[str, Any]:
        kind: str = self.kind

        runtime_growth_bytes = self.runtime_growth_bytes

        startup_peak_bytes = self.startup_peak_bytes

        steady_state_bytes = self.steady_state_bytes

        system_reserve_bytes = self.system_reserve_bytes


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "kind": kind,
            "runtime_growth_bytes": runtime_growth_bytes,
            "startup_peak_bytes": startup_peak_bytes,
            "steady_state_bytes": steady_state_bytes,
            "system_reserve_bytes": system_reserve_bytes,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        kind = check_recipe_memory_requirements_kind(d.pop("kind"))




        runtime_growth_bytes = d.pop("runtime_growth_bytes")

        startup_peak_bytes = d.pop("startup_peak_bytes")

        steady_state_bytes = d.pop("steady_state_bytes")

        system_reserve_bytes = d.pop("system_reserve_bytes")

        recipe_memory_requirements = cls(
            kind=kind,
            runtime_growth_bytes=runtime_growth_bytes,
            startup_peak_bytes=startup_peak_bytes,
            steady_state_bytes=steady_state_bytes,
            system_reserve_bytes=system_reserve_bytes,
        )

        return recipe_memory_requirements
