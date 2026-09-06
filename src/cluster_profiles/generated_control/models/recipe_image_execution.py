from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast
from typing import Literal, cast

if TYPE_CHECKING:
  from ..models.recipe_image import RecipeImage





T = TypeVar("T", bound="RecipeImageExecution")



@_attrs_define
class RecipeImageExecution:
    """
        Attributes:
            image (RecipeImage):
            mode (Literal['image']):
     """

    image: 'RecipeImage'
    mode: Literal['image']





    def to_dict(self) -> dict[str, Any]:
        from ..models.recipe_image import RecipeImage
        image = self.image.to_dict()

        mode = self.mode


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "image": image,
            "mode": mode,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.recipe_image import RecipeImage
        d = dict(src_dict)
        image = RecipeImage.from_dict(d.pop("image"))




        mode = cast(Literal['image'] , d.pop("mode"))
        if mode != 'image':
            raise ValueError(f"mode must match const 'image', got '{mode}'")

        recipe_image_execution = cls(
            image=image,
            mode=mode,
        )

        return recipe_image_execution
