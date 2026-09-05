from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast, Union
from typing import Union






T = TypeVar("T", bound="CompatibilityIdentity")



@_attrs_define
class CompatibilityIdentity:
    """ Immutable inputs for an exceptional reusable preparation artifact.

        Attributes:
            model_version_sha256 (str):
            parameters_sha256 (str):
            recipe_revision_sha256 (str):
            runtime_image_digest (str):
            hardware_profile_sha256 (Union[None, Unset, str]):
     """

    model_version_sha256: str
    parameters_sha256: str
    recipe_revision_sha256: str
    runtime_image_digest: str
    hardware_profile_sha256: Union[None, Unset, str] = UNSET





    def to_dict(self) -> dict[str, Any]:
        model_version_sha256 = self.model_version_sha256

        parameters_sha256 = self.parameters_sha256

        recipe_revision_sha256 = self.recipe_revision_sha256

        runtime_image_digest = self.runtime_image_digest

        hardware_profile_sha256: Union[None, Unset, str]
        if isinstance(self.hardware_profile_sha256, Unset):
            hardware_profile_sha256 = UNSET
        else:
            hardware_profile_sha256 = self.hardware_profile_sha256


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "model_version_sha256": model_version_sha256,
            "parameters_sha256": parameters_sha256,
            "recipe_revision_sha256": recipe_revision_sha256,
            "runtime_image_digest": runtime_image_digest,
        })
        if hardware_profile_sha256 is not UNSET:
            field_dict["hardware_profile_sha256"] = hardware_profile_sha256

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        model_version_sha256 = d.pop("model_version_sha256")

        parameters_sha256 = d.pop("parameters_sha256")

        recipe_revision_sha256 = d.pop("recipe_revision_sha256")

        runtime_image_digest = d.pop("runtime_image_digest")

        def _parse_hardware_profile_sha256(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        hardware_profile_sha256 = _parse_hardware_profile_sha256(d.pop("hardware_profile_sha256", UNSET))


        compatibility_identity = cls(
            model_version_sha256=model_version_sha256,
            parameters_sha256=parameters_sha256,
            recipe_revision_sha256=recipe_revision_sha256,
            runtime_image_digest=runtime_image_digest,
            hardware_profile_sha256=hardware_profile_sha256,
        )

        return compatibility_identity
