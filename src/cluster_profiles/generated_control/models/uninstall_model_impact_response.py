from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast






T = TypeVar("T", bound="UninstallModelImpactResponse")



@_attrs_define
class UninstallModelImpactResponse:
    """
        Attributes:
            cleanup_node_ids (list[str]):
            dependent_recipe_ids (list[str]):
            effect (str):
            model_title (str):
            model_version_sha256 (str):
            retained_node_ids (list[str]):
     """

    cleanup_node_ids: list[str]
    dependent_recipe_ids: list[str]
    effect: str
    model_title: str
    model_version_sha256: str
    retained_node_ids: list[str]





    def to_dict(self) -> dict[str, Any]:
        cleanup_node_ids = self.cleanup_node_ids



        dependent_recipe_ids = self.dependent_recipe_ids



        effect = self.effect

        model_title = self.model_title

        model_version_sha256 = self.model_version_sha256

        retained_node_ids = self.retained_node_ids




        field_dict: dict[str, Any] = {}

        field_dict.update({
            "cleanup_node_ids": cleanup_node_ids,
            "dependent_recipe_ids": dependent_recipe_ids,
            "effect": effect,
            "model_title": model_title,
            "model_version_sha256": model_version_sha256,
            "retained_node_ids": retained_node_ids,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        cleanup_node_ids = cast(list[str], d.pop("cleanup_node_ids"))


        dependent_recipe_ids = cast(list[str], d.pop("dependent_recipe_ids"))


        effect = d.pop("effect")

        model_title = d.pop("model_title")

        model_version_sha256 = d.pop("model_version_sha256")

        retained_node_ids = cast(list[str], d.pop("retained_node_ids"))


        uninstall_model_impact_response = cls(
            cleanup_node_ids=cleanup_node_ids,
            dependent_recipe_ids=dependent_recipe_ids,
            effect=effect,
            model_title=model_title,
            model_version_sha256=model_version_sha256,
            retained_node_ids=retained_node_ids,
        )

        return uninstall_model_impact_response
