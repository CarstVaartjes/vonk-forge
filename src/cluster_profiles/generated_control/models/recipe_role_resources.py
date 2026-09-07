from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast

if TYPE_CHECKING:
  from ..models.recipe_memory_resources import RecipeMemoryResources
  from ..models.recipe_disk_resources import RecipeDiskResources





T = TypeVar("T", bound="RecipeRoleResources")



@_attrs_define
class RecipeRoleResources:
    """
        Attributes:
            disk (RecipeDiskResources):
            memory (RecipeMemoryResources):
     """

    disk: 'RecipeDiskResources'
    memory: 'RecipeMemoryResources'





    def to_dict(self) -> dict[str, Any]:
        from ..models.recipe_memory_resources import RecipeMemoryResources
        from ..models.recipe_disk_resources import RecipeDiskResources
        disk = self.disk.to_dict()

        memory = self.memory.to_dict()


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "disk": disk,
            "memory": memory,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.recipe_memory_resources import RecipeMemoryResources
        from ..models.recipe_disk_resources import RecipeDiskResources
        d = dict(src_dict)
        disk = RecipeDiskResources.from_dict(d.pop("disk"))




        memory = RecipeMemoryResources.from_dict(d.pop("memory"))




        recipe_role_resources = cls(
            disk=disk,
            memory=memory,
        )

        return recipe_role_resources
