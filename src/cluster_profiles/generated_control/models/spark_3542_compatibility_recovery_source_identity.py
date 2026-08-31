from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import Literal, cast






T = TypeVar("T", bound="Spark3542CompatibilityRecoverySourceIdentity")



@_attrs_define
class Spark3542CompatibilityRecoverySourceIdentity:
    """
        Attributes:
            binary_digest (str):
            build_digest (str):
            semantic_version (Literal['0.1.0']):
     """

    binary_digest: str
    build_digest: str
    semantic_version: Literal['0.1.0']





    def to_dict(self) -> dict[str, Any]:
        binary_digest = self.binary_digest

        build_digest = self.build_digest

        semantic_version = self.semantic_version


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "binary_digest": binary_digest,
            "build_digest": build_digest,
            "semantic_version": semantic_version,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        binary_digest = d.pop("binary_digest")

        build_digest = d.pop("build_digest")

        semantic_version = cast(Literal['0.1.0'] , d.pop("semantic_version"))
        if semantic_version != '0.1.0':
            raise ValueError(f"semantic_version must match const '0.1.0', got '{semantic_version}'")

        spark_3542_compatibility_recovery_source_identity = cls(
            binary_digest=binary_digest,
            build_digest=build_digest,
            semantic_version=semantic_version,
        )

        return spark_3542_compatibility_recovery_source_identity
