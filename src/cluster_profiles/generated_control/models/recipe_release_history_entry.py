from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.recipe_release_history_entry_upgrade_effect import check_recipe_release_history_entry_upgrade_effect
from ..models.recipe_release_history_entry_upgrade_effect import RecipeReleaseHistoryEntryUpgradeEffect
from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Union

if TYPE_CHECKING:
  from ..models.recipe_release_change import RecipeReleaseChange





T = TypeVar("T", bound="RecipeReleaseHistoryEntry")



@_attrs_define
class RecipeReleaseHistoryEntry:
    """
        Attributes:
            changes (list['RecipeReleaseChange']):
            released_at (str):
            upgrade_effect (RecipeReleaseHistoryEntryUpgradeEffect):
            version (str):
            prior_recipe_content_sha256 (Union[None, Unset, str]):
     """

    changes: list['RecipeReleaseChange']
    released_at: str
    upgrade_effect: RecipeReleaseHistoryEntryUpgradeEffect
    version: str
    prior_recipe_content_sha256: Union[None, Unset, str] = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.recipe_release_change import RecipeReleaseChange
        changes = []
        for changes_item_data in self.changes:
            changes_item = changes_item_data.to_dict()
            changes.append(changes_item)



        released_at = self.released_at

        upgrade_effect: str = self.upgrade_effect

        version = self.version

        prior_recipe_content_sha256: Union[None, Unset, str]
        if isinstance(self.prior_recipe_content_sha256, Unset):
            prior_recipe_content_sha256 = UNSET
        else:
            prior_recipe_content_sha256 = self.prior_recipe_content_sha256


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "changes": changes,
            "released_at": released_at,
            "upgrade_effect": upgrade_effect,
            "version": version,
        })
        if prior_recipe_content_sha256 is not UNSET:
            field_dict["prior_recipe_content_sha256"] = prior_recipe_content_sha256

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.recipe_release_change import RecipeReleaseChange
        d = dict(src_dict)
        changes = []
        _changes = d.pop("changes")
        for changes_item_data in (_changes):
            changes_item = RecipeReleaseChange.from_dict(changes_item_data)



            changes.append(changes_item)


        released_at = d.pop("released_at")

        upgrade_effect = check_recipe_release_history_entry_upgrade_effect(d.pop("upgrade_effect"))




        version = d.pop("version")

        def _parse_prior_recipe_content_sha256(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        prior_recipe_content_sha256 = _parse_prior_recipe_content_sha256(d.pop("prior_recipe_content_sha256", UNSET))


        recipe_release_history_entry = cls(
            changes=changes,
            released_at=released_at,
            upgrade_effect=upgrade_effect,
            version=version,
            prior_recipe_content_sha256=prior_recipe_content_sha256,
        )

        return recipe_release_history_entry
