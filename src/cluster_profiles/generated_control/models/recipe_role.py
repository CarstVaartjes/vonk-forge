from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast

if TYPE_CHECKING:
  from ..models.recipe_memory_requirements import RecipeMemoryRequirements
  from ..models.recipe_disk_requirements import RecipeDiskRequirements





T = TypeVar("T", bound="RecipeRole")



@_attrs_define
class RecipeRole:
    """
        Attributes:
            artifacts (list[str]):
            count (int):
            disk (RecipeDiskRequirements):
            endpoint_owner (bool):
            memory (RecipeMemoryRequirements):
            name (str):
     """

    artifacts: list[str]
    count: int
    disk: 'RecipeDiskRequirements'
    endpoint_owner: bool
    memory: 'RecipeMemoryRequirements'
    name: str





    def to_dict(self) -> dict[str, Any]:
        from ..models.recipe_memory_requirements import RecipeMemoryRequirements
        from ..models.recipe_disk_requirements import RecipeDiskRequirements
        artifacts = self.artifacts



        count = self.count

        disk = self.disk.to_dict()

        endpoint_owner = self.endpoint_owner

        memory = self.memory.to_dict()

        name = self.name


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "artifacts": artifacts,
            "count": count,
            "disk": disk,
            "endpoint_owner": endpoint_owner,
            "memory": memory,
            "name": name,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.recipe_memory_requirements import RecipeMemoryRequirements
        from ..models.recipe_disk_requirements import RecipeDiskRequirements
        d = dict(src_dict)
        artifacts = cast(list[str], d.pop("artifacts"))


        count = d.pop("count")

        disk = RecipeDiskRequirements.from_dict(d.pop("disk"))




        endpoint_owner = d.pop("endpoint_owner")

        memory = RecipeMemoryRequirements.from_dict(d.pop("memory"))




        name = d.pop("name")

        recipe_role = cls(
            artifacts=artifacts,
            count=count,
            disk=disk,
            endpoint_owner=endpoint_owner,
            memory=memory,
            name=name,
        )

        return recipe_role
