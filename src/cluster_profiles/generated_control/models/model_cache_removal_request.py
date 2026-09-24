from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import Literal, Union, cast






T = TypeVar("T", bound="ModelCacheRemovalRequest")



@_attrs_define
class ModelCacheRemovalRequest:
    """ Exact content identity and request key for a model cache removal.

        Attributes:
            model_content_sha256 (str):
            request_key (str):
            review_digest (str):
            schema_version (Union[Literal[2], Unset]):  Default: 2.
     """

    model_content_sha256: str
    request_key: str
    review_digest: str
    schema_version: Union[Literal[2], Unset] = 2





    def to_dict(self) -> dict[str, Any]:
        model_content_sha256 = self.model_content_sha256

        request_key = self.request_key

        review_digest = self.review_digest

        schema_version = self.schema_version


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "model_content_sha256": model_content_sha256,
            "request_key": request_key,
            "review_digest": review_digest,
        })
        if schema_version is not UNSET:
            field_dict["schema_version"] = schema_version

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        model_content_sha256 = d.pop("model_content_sha256")

        request_key = d.pop("request_key")

        review_digest = d.pop("review_digest")

        schema_version = cast(Union[Literal[2], Unset] , d.pop("schema_version", UNSET))
        if schema_version != 2 and not isinstance(schema_version, Unset):
            raise ValueError(f"schema_version must match const 2, got '{schema_version}'")

        model_cache_removal_request = cls(
            model_content_sha256=model_content_sha256,
            request_key=request_key,
            review_digest=review_digest,
            schema_version=schema_version,
        )

        return model_cache_removal_request
