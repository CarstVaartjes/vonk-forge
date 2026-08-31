from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast






T = TypeVar("T", bound="ModelDeletionInstallationImpactResponse")



@_attrs_define
class ModelDeletionInstallationImpactResponse:
    """
        Attributes:
            installation_id (str):
            installed_bytes (int):
            node_ids (list[str]):
            recipe_content_sha256 (str):
            recipe_id (str):
            recipe_revision_id (str):
     """

    installation_id: str
    installed_bytes: int
    node_ids: list[str]
    recipe_content_sha256: str
    recipe_id: str
    recipe_revision_id: str





    def to_dict(self) -> dict[str, Any]:
        installation_id = self.installation_id

        installed_bytes = self.installed_bytes

        node_ids = self.node_ids



        recipe_content_sha256 = self.recipe_content_sha256

        recipe_id = self.recipe_id

        recipe_revision_id = self.recipe_revision_id


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "installation_id": installation_id,
            "installed_bytes": installed_bytes,
            "node_ids": node_ids,
            "recipe_content_sha256": recipe_content_sha256,
            "recipe_id": recipe_id,
            "recipe_revision_id": recipe_revision_id,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        installation_id = d.pop("installation_id")

        installed_bytes = d.pop("installed_bytes")

        node_ids = cast(list[str], d.pop("node_ids"))


        recipe_content_sha256 = d.pop("recipe_content_sha256")

        recipe_id = d.pop("recipe_id")

        recipe_revision_id = d.pop("recipe_revision_id")

        model_deletion_installation_impact_response = cls(
            installation_id=installation_id,
            installed_bytes=installed_bytes,
            node_ids=node_ids,
            recipe_content_sha256=recipe_content_sha256,
            recipe_id=recipe_id,
            recipe_revision_id=recipe_revision_id,
        )

        return model_deletion_installation_impact_response
