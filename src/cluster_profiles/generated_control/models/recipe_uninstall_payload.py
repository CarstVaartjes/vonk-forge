from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast






T = TypeVar("T", bound="RecipeUninstallPayload")



@_attrs_define
class RecipeUninstallPayload:
    """
        Attributes:
            cleanup_model_content_sha256 (None | str):
            installation_id (str):
            plan_digest (str):
            recipe_content_sha256 (str):
     """

    cleanup_model_content_sha256: None | str
    installation_id: str
    plan_digest: str
    recipe_content_sha256: str





    def to_dict(self) -> dict[str, Any]:
        cleanup_model_content_sha256: None | str
        cleanup_model_content_sha256 = self.cleanup_model_content_sha256

        installation_id = self.installation_id

        plan_digest = self.plan_digest

        recipe_content_sha256 = self.recipe_content_sha256


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "cleanup_model_content_sha256": cleanup_model_content_sha256,
            "installation_id": installation_id,
            "plan_digest": plan_digest,
            "recipe_content_sha256": recipe_content_sha256,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        def _parse_cleanup_model_content_sha256(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        cleanup_model_content_sha256 = _parse_cleanup_model_content_sha256(d.pop("cleanup_model_content_sha256"))


        installation_id = d.pop("installation_id")

        plan_digest = d.pop("plan_digest")

        recipe_content_sha256 = d.pop("recipe_content_sha256")

        recipe_uninstall_payload = cls(
            cleanup_model_content_sha256=cleanup_model_content_sha256,
            installation_id=installation_id,
            plan_digest=plan_digest,
            recipe_content_sha256=recipe_content_sha256,
        )

        return recipe_uninstall_payload
