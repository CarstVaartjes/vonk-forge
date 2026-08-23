from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.public_recipe_release_upgrade_effect import check_public_recipe_release_upgrade_effect
from ..models.public_recipe_release_upgrade_effect import PublicRecipeReleaseUpgradeEffect
from typing import cast

if TYPE_CHECKING:
  from ..models.public_recipe_change import PublicRecipeChange





T = TypeVar("T", bound="PublicRecipeRelease")



@_attrs_define
class PublicRecipeRelease:
    """
        Attributes:
            changes (list['PublicRecipeChange']):
            content_sha256 (str):
            released_at (str):
            upgrade_effect (PublicRecipeReleaseUpgradeEffect):
            version (str):
     """

    changes: list['PublicRecipeChange']
    content_sha256: str
    released_at: str
    upgrade_effect: PublicRecipeReleaseUpgradeEffect
    version: str





    def to_dict(self) -> dict[str, Any]:
        from ..models.public_recipe_change import PublicRecipeChange
        changes = []
        for changes_item_data in self.changes:
            changes_item = changes_item_data.to_dict()
            changes.append(changes_item)



        content_sha256 = self.content_sha256

        released_at = self.released_at

        upgrade_effect: str = self.upgrade_effect

        version = self.version


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "changes": changes,
            "content_sha256": content_sha256,
            "released_at": released_at,
            "upgrade_effect": upgrade_effect,
            "version": version,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.public_recipe_change import PublicRecipeChange
        d = dict(src_dict)
        changes = []
        _changes = d.pop("changes")
        for changes_item_data in (_changes):
            changes_item = PublicRecipeChange.from_dict(changes_item_data)



            changes.append(changes_item)


        content_sha256 = d.pop("content_sha256")

        released_at = d.pop("released_at")

        upgrade_effect = check_public_recipe_release_upgrade_effect(d.pop("upgrade_effect"))




        version = d.pop("version")

        public_recipe_release = cls(
            changes=changes,
            content_sha256=content_sha256,
            released_at=released_at,
            upgrade_effect=upgrade_effect,
            version=version,
        )

        return public_recipe_release
