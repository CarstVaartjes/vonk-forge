from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast, Union






T = TypeVar("T", bound="CompiledIdentity")



@_attrs_define
class CompiledIdentity:
    """
        Attributes:
            build_input_sha256 (Union[None, str]):
            execution_sha256 (str):
            harness_sha256 (str):
            model_artifact_bytes (int):
            model_artifact_set_sha256 (str):
            recipe_revision_sha256 (str):
     """

    build_input_sha256: Union[None, str]
    execution_sha256: str
    harness_sha256: str
    model_artifact_bytes: int
    model_artifact_set_sha256: str
    recipe_revision_sha256: str





    def to_dict(self) -> dict[str, Any]:
        build_input_sha256: Union[None, str]
        build_input_sha256 = self.build_input_sha256

        execution_sha256 = self.execution_sha256

        harness_sha256 = self.harness_sha256

        model_artifact_bytes = self.model_artifact_bytes

        model_artifact_set_sha256 = self.model_artifact_set_sha256

        recipe_revision_sha256 = self.recipe_revision_sha256


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "build_input_sha256": build_input_sha256,
            "execution_sha256": execution_sha256,
            "harness_sha256": harness_sha256,
            "model_artifact_bytes": model_artifact_bytes,
            "model_artifact_set_sha256": model_artifact_set_sha256,
            "recipe_revision_sha256": recipe_revision_sha256,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        def _parse_build_input_sha256(data: object) -> Union[None, str]:
            if data is None:
                return data
            return cast(Union[None, str], data)

        build_input_sha256 = _parse_build_input_sha256(d.pop("build_input_sha256"))


        execution_sha256 = d.pop("execution_sha256")

        harness_sha256 = d.pop("harness_sha256")

        model_artifact_bytes = d.pop("model_artifact_bytes")

        model_artifact_set_sha256 = d.pop("model_artifact_set_sha256")

        recipe_revision_sha256 = d.pop("recipe_revision_sha256")

        compiled_identity = cls(
            build_input_sha256=build_input_sha256,
            execution_sha256=execution_sha256,
            harness_sha256=harness_sha256,
            model_artifact_bytes=model_artifact_bytes,
            model_artifact_set_sha256=model_artifact_set_sha256,
            recipe_revision_sha256=recipe_revision_sha256,
        )

        return compiled_identity
