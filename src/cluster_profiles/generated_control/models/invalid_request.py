from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.invalid_request_reason import check_invalid_request_reason
from ..models.invalid_request_reason import InvalidRequestReason
from ..types import UNSET, Unset
from typing import cast
from typing import Literal, cast






T = TypeVar("T", bound="InvalidRequest")



@_attrs_define
class InvalidRequest:
    """ A malformed or out-of-contract request; it fails closed at submit time.

        Attributes:
            category (Literal['invalid-request']):
            reason (InvalidRequestReason): Closed reason codes of an invalid request (submit-time input validation).
            field (None | str | Unset):
     """

    category: Literal['invalid-request']
    reason: InvalidRequestReason
    field: None | str | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        category = self.category

        reason: str = self.reason

        field: None | str | Unset
        if isinstance(self.field, Unset):
            field = UNSET
        else:
            field = self.field


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "category": category,
            "reason": reason,
        })
        if field is not UNSET:
            field_dict["field"] = field

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        category = cast(Literal['invalid-request'] , d.pop("category"))
        if category != 'invalid-request':
            raise ValueError(f"category must match const 'invalid-request', got '{category}'")

        reason = check_invalid_request_reason(d.pop("reason"))




        def _parse_field(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        field = _parse_field(d.pop("field", UNSET))


        invalid_request = cls(
            category=category,
            reason=reason,
            field=field,
        )

        return invalid_request
