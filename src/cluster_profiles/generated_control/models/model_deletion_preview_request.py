from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="ModelDeletionPreviewRequest")



@_attrs_define
class ModelDeletionPreviewRequest:
    """
        Attributes:
            model_version_sha256 (str):
     """

    model_version_sha256: str





    def to_dict(self) -> dict[str, Any]:
        model_version_sha256 = self.model_version_sha256


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "model_version_sha256": model_version_sha256,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        model_version_sha256 = d.pop("model_version_sha256")

        model_deletion_preview_request = cls(
            model_version_sha256=model_version_sha256,
        )

        return model_deletion_preview_request
