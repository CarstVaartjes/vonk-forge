from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast, Union
from typing import Literal, Union, cast
from typing import Union






T = TypeVar("T", bound="RecipeOperatorRequest")



@_attrs_define
class RecipeOperatorRequest:
    """
        Attributes:
            request_key (str):
            with_model (bool):
            review_digest (Union[None, Unset, str]):
            schema_version (Union[Literal[2], Unset]):  Default: 2.
     """

    request_key: str
    with_model: bool
    review_digest: Union[None, Unset, str] = UNSET
    schema_version: Union[Literal[2], Unset] = 2





    def to_dict(self) -> dict[str, Any]:
        request_key = self.request_key

        with_model = self.with_model

        review_digest: Union[None, Unset, str]
        if isinstance(self.review_digest, Unset):
            review_digest = UNSET
        else:
            review_digest = self.review_digest

        schema_version = self.schema_version


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "request_key": request_key,
            "with_model": with_model,
        })
        if review_digest is not UNSET:
            field_dict["review_digest"] = review_digest
        if schema_version is not UNSET:
            field_dict["schema_version"] = schema_version

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        request_key = d.pop("request_key")

        with_model = d.pop("with_model")

        def _parse_review_digest(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        review_digest = _parse_review_digest(d.pop("review_digest", UNSET))


        schema_version = cast(Union[Literal[2], Unset] , d.pop("schema_version", UNSET))
        if schema_version != 2 and not isinstance(schema_version, Unset):
            raise ValueError(f"schema_version must match const 2, got '{schema_version}'")

        recipe_operator_request = cls(
            request_key=request_key,
            with_model=with_model,
            review_digest=review_digest,
            schema_version=schema_version,
        )

        return recipe_operator_request
