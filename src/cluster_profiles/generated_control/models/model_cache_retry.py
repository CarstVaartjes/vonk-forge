from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast






T = TypeVar("T", bound="ModelCacheRetry")



@_attrs_define
class ModelCacheRetry:
    """
        Attributes:
            automatic_attempts (int):
            operator_retries (int):
            credential_fingerprint (None | str | Unset):
            next_retry_at (None | str | Unset):
            retry_after_seconds (int | None | Unset):
     """

    automatic_attempts: int
    operator_retries: int
    credential_fingerprint: None | str | Unset = UNSET
    next_retry_at: None | str | Unset = UNSET
    retry_after_seconds: int | None | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        automatic_attempts = self.automatic_attempts

        operator_retries = self.operator_retries

        credential_fingerprint: None | str | Unset
        if isinstance(self.credential_fingerprint, Unset):
            credential_fingerprint = UNSET
        else:
            credential_fingerprint = self.credential_fingerprint

        next_retry_at: None | str | Unset
        if isinstance(self.next_retry_at, Unset):
            next_retry_at = UNSET
        else:
            next_retry_at = self.next_retry_at

        retry_after_seconds: int | None | Unset
        if isinstance(self.retry_after_seconds, Unset):
            retry_after_seconds = UNSET
        else:
            retry_after_seconds = self.retry_after_seconds


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "automatic_attempts": automatic_attempts,
            "operator_retries": operator_retries,
        })
        if credential_fingerprint is not UNSET:
            field_dict["credential_fingerprint"] = credential_fingerprint
        if next_retry_at is not UNSET:
            field_dict["next_retry_at"] = next_retry_at
        if retry_after_seconds is not UNSET:
            field_dict["retry_after_seconds"] = retry_after_seconds

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        automatic_attempts = d.pop("automatic_attempts")

        operator_retries = d.pop("operator_retries")

        def _parse_credential_fingerprint(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        credential_fingerprint = _parse_credential_fingerprint(d.pop("credential_fingerprint", UNSET))


        def _parse_next_retry_at(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        next_retry_at = _parse_next_retry_at(d.pop("next_retry_at", UNSET))


        def _parse_retry_after_seconds(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        retry_after_seconds = _parse_retry_after_seconds(d.pop("retry_after_seconds", UNSET))


        model_cache_retry = cls(
            automatic_attempts=automatic_attempts,
            operator_retries=operator_retries,
            credential_fingerprint=credential_fingerprint,
            next_retry_at=next_retry_at,
            retry_after_seconds=retry_after_seconds,
        )

        return model_cache_retry
