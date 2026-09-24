from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast

if TYPE_CHECKING:
  from ..models.recipe_build_policy import RecipeBuildPolicy





T = TypeVar("T", bound="RecipeBuildEvidence")



@_attrs_define
class RecipeBuildEvidence:
    """
        Attributes:
            build_input_sha256 (str):
            image_bytes (int):
            image_digest (str):
            oci_layout_sha256 (str):
            policy (RecipeBuildPolicy):
     """

    build_input_sha256: str
    image_bytes: int
    image_digest: str
    oci_layout_sha256: str
    policy: 'RecipeBuildPolicy'





    def to_dict(self) -> dict[str, Any]:
        from ..models.recipe_build_policy import RecipeBuildPolicy
        build_input_sha256 = self.build_input_sha256

        image_bytes = self.image_bytes

        image_digest = self.image_digest

        oci_layout_sha256 = self.oci_layout_sha256

        policy = self.policy.to_dict()


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "build_input_sha256": build_input_sha256,
            "image_bytes": image_bytes,
            "image_digest": image_digest,
            "oci_layout_sha256": oci_layout_sha256,
            "policy": policy,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.recipe_build_policy import RecipeBuildPolicy
        d = dict(src_dict)
        build_input_sha256 = d.pop("build_input_sha256")

        image_bytes = d.pop("image_bytes")

        image_digest = d.pop("image_digest")

        oci_layout_sha256 = d.pop("oci_layout_sha256")

        policy = RecipeBuildPolicy.from_dict(d.pop("policy"))




        recipe_build_evidence = cls(
            build_input_sha256=build_input_sha256,
            image_bytes=image_bytes,
            image_digest=image_digest,
            oci_layout_sha256=oci_layout_sha256,
            policy=policy,
        )

        return recipe_build_evidence
