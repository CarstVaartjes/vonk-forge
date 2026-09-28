from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
from typing import Literal, cast






T = TypeVar("T", bound="ModelCacheRemovalRequest")



@_attrs_define
class ModelCacheRemovalRequest:
    """ Request key for removing the named model against current state.

    ``model_content_sha256`` and ``review_digest`` are accepted for clients
    that show a prior review; they are advisory and never refuse the request.

        Attributes:
            request_key (str):
            model_content_sha256 (None | str | Unset):
            review_digest (None | str | Unset):
            schema_version (Literal[2] | Unset):  Default: 2.
     """

    request_key: str
    model_content_sha256: None | str | Unset = UNSET
    review_digest: None | str | Unset = UNSET
    schema_version: Literal[2] | Unset = 2





    def to_dict(self) -> dict[str, Any]:
        request_key = self.request_key

        model_content_sha256: None | str | Unset
        if isinstance(self.model_content_sha256, Unset):
            model_content_sha256 = UNSET
        else:
            model_content_sha256 = self.model_content_sha256

        review_digest: None | str | Unset
        if isinstance(self.review_digest, Unset):
            review_digest = UNSET
        else:
            review_digest = self.review_digest

        schema_version = self.schema_version


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "request_key": request_key,
        })
        if model_content_sha256 is not UNSET:
            field_dict["model_content_sha256"] = model_content_sha256
        if review_digest is not UNSET:
            field_dict["review_digest"] = review_digest
        if schema_version is not UNSET:
            field_dict["schema_version"] = schema_version

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        request_key = d.pop("request_key")

        def _parse_model_content_sha256(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        model_content_sha256 = _parse_model_content_sha256(d.pop("model_content_sha256", UNSET))


        def _parse_review_digest(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        review_digest = _parse_review_digest(d.pop("review_digest", UNSET))


        schema_version = cast(Literal[2] | Unset , d.pop("schema_version", UNSET))
        if schema_version != 2 and not isinstance(schema_version, Unset):
            raise ValueError(f"schema_version must match const 2, got '{schema_version}'")

        model_cache_removal_request = cls(
            request_key=request_key,
            model_content_sha256=model_content_sha256,
            review_digest=review_digest,
            schema_version=schema_version,
        )

        return model_cache_removal_request
