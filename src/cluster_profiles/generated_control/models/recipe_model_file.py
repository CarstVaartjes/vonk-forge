from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast

if TYPE_CHECKING:
  from ..models.recipe_mount import RecipeMount





T = TypeVar("T", bound="RecipeModelFile")



@_attrs_define
class RecipeModelFile:
    """
        Attributes:
            file_id (str):
            id (str):
            mount (RecipeMount):
            roles (list[str]):
     """

    file_id: str
    id: str
    mount: 'RecipeMount'
    roles: list[str]





    def to_dict(self) -> dict[str, Any]:
        from ..models.recipe_mount import RecipeMount
        file_id = self.file_id

        id = self.id

        mount = self.mount.to_dict()

        roles = self.roles




        field_dict: dict[str, Any] = {}

        field_dict.update({
            "file_id": file_id,
            "id": id,
            "mount": mount,
            "roles": roles,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.recipe_mount import RecipeMount
        d = dict(src_dict)
        file_id = d.pop("file_id")

        id = d.pop("id")

        mount = RecipeMount.from_dict(d.pop("mount"))




        roles = cast(list[str], d.pop("roles"))


        recipe_model_file = cls(
            file_id=file_id,
            id=id,
            mount=mount,
            roles=roles,
        )

        return recipe_model_file
