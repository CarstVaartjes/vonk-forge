from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast

if TYPE_CHECKING:
  from ..models.recipe_release_history_entry import RecipeReleaseHistoryEntry





T = TypeVar("T", bound="RecipeRelease")



@_attrs_define
class RecipeRelease:
    """
        Attributes:
            history (list['RecipeReleaseHistoryEntry']):
            released_at (str):
            version (str):
     """

    history: list['RecipeReleaseHistoryEntry']
    released_at: str
    version: str





    def to_dict(self) -> dict[str, Any]:
        from ..models.recipe_release_history_entry import RecipeReleaseHistoryEntry
        history = []
        for history_item_data in self.history:
            history_item = history_item_data.to_dict()
            history.append(history_item)



        released_at = self.released_at

        version = self.version


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "history": history,
            "released_at": released_at,
            "version": version,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.recipe_release_history_entry import RecipeReleaseHistoryEntry
        d = dict(src_dict)
        history = []
        _history = d.pop("history")
        for history_item_data in (_history):
            history_item = RecipeReleaseHistoryEntry.from_dict(history_item_data)



            history.append(history_item)


        released_at = d.pop("released_at")

        version = d.pop("version")

        recipe_release = cls(
            history=history,
            released_at=released_at,
            version=version,
        )

        return recipe_release
