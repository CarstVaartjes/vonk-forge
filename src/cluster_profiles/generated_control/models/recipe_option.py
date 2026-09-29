from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast

if TYPE_CHECKING:
  from ..models.recipe_option_choice import RecipeOptionChoice





T = TypeVar("T", bound="RecipeOption")



@_attrs_define
class RecipeOption:
    """ A recipe-declared setting with a fixed set of named values.

    Users pick one of the enumerated choices; there are no free-form values.
    Exactly one choice is the default and applies whenever nothing is chosen.

        Attributes:
            choices (list[RecipeOptionChoice]):
            help_ (str):
            label (str):
            name (str):
     """

    choices: list[RecipeOptionChoice]
    help_: str
    label: str
    name: str





    def to_dict(self) -> dict[str, Any]:
        from ..models.recipe_option_choice import RecipeOptionChoice # noqa: PLC0415
        choices = []
        for choices_item_data in self.choices:
            choices_item = choices_item_data.to_dict()
            choices.append(choices_item)



        help_ = self.help_

        label = self.label

        name = self.name


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "choices": choices,
            "help": help_,
            "label": label,
            "name": name,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.recipe_option_choice import RecipeOptionChoice # noqa: PLC0415
        d = dict(src_dict)
        choices = []
        _choices = d.pop("choices")
        for choices_item_data in (_choices):
            choices_item = RecipeOptionChoice.from_dict(choices_item_data)



            choices.append(choices_item)


        help_ = d.pop("help")

        label = d.pop("label")

        name = d.pop("name")

        recipe_option = cls(
            choices=choices,
            help_=help_,
            label=label,
            name=name,
        )

        return recipe_option
