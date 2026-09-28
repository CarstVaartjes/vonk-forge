from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.availability_recovery_action import AvailabilityRecoveryAction
from ..models.availability_recovery_action import check_availability_recovery_action
from ..types import UNSET, Unset
from typing import cast






T = TypeVar("T", bound="AvailabilityOperationFailure")



@_attrs_define
class AvailabilityOperationFailure:
    """ Shared failure wire contract for model and image availability.

        Attributes:
            code (str):
            detail (str):
            artifact_key (None | str | Unset):
            free_bytes (int | None | Unset):
            log_excerpt (None | str | Unset):
            recovery_actions (list[AvailabilityRecoveryAction] | Unset):
            required_bytes (int | None | Unset):
            retry_after_seconds (int | None | Unset):
            retry_time (None | str | Unset):
            retryable (bool | Unset):  Default: False.
            shortfall_bytes (int | None | Unset):
     """

    code: str
    detail: str
    artifact_key: None | str | Unset = UNSET
    free_bytes: int | None | Unset = UNSET
    log_excerpt: None | str | Unset = UNSET
    recovery_actions: list[AvailabilityRecoveryAction] | Unset = UNSET
    required_bytes: int | None | Unset = UNSET
    retry_after_seconds: int | None | Unset = UNSET
    retry_time: None | str | Unset = UNSET
    retryable: bool | Unset = False
    shortfall_bytes: int | None | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        code = self.code

        detail = self.detail

        artifact_key: None | str | Unset
        if isinstance(self.artifact_key, Unset):
            artifact_key = UNSET
        else:
            artifact_key = self.artifact_key

        free_bytes: int | None | Unset
        if isinstance(self.free_bytes, Unset):
            free_bytes = UNSET
        else:
            free_bytes = self.free_bytes

        log_excerpt: None | str | Unset
        if isinstance(self.log_excerpt, Unset):
            log_excerpt = UNSET
        else:
            log_excerpt = self.log_excerpt

        recovery_actions: list[str] | Unset = UNSET
        if not isinstance(self.recovery_actions, Unset):
            recovery_actions = []
            for recovery_actions_item_data in self.recovery_actions:
                recovery_actions_item: str = recovery_actions_item_data
                recovery_actions.append(recovery_actions_item)



        required_bytes: int | None | Unset
        if isinstance(self.required_bytes, Unset):
            required_bytes = UNSET
        else:
            required_bytes = self.required_bytes

        retry_after_seconds: int | None | Unset
        if isinstance(self.retry_after_seconds, Unset):
            retry_after_seconds = UNSET
        else:
            retry_after_seconds = self.retry_after_seconds

        retry_time: None | str | Unset
        if isinstance(self.retry_time, Unset):
            retry_time = UNSET
        else:
            retry_time = self.retry_time

        retryable = self.retryable

        shortfall_bytes: int | None | Unset
        if isinstance(self.shortfall_bytes, Unset):
            shortfall_bytes = UNSET
        else:
            shortfall_bytes = self.shortfall_bytes


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "code": code,
            "detail": detail,
        })
        if artifact_key is not UNSET:
            field_dict["artifact_key"] = artifact_key
        if free_bytes is not UNSET:
            field_dict["free_bytes"] = free_bytes
        if log_excerpt is not UNSET:
            field_dict["log_excerpt"] = log_excerpt
        if recovery_actions is not UNSET:
            field_dict["recovery_actions"] = recovery_actions
        if required_bytes is not UNSET:
            field_dict["required_bytes"] = required_bytes
        if retry_after_seconds is not UNSET:
            field_dict["retry_after_seconds"] = retry_after_seconds
        if retry_time is not UNSET:
            field_dict["retry_time"] = retry_time
        if retryable is not UNSET:
            field_dict["retryable"] = retryable
        if shortfall_bytes is not UNSET:
            field_dict["shortfall_bytes"] = shortfall_bytes

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        code = d.pop("code")

        detail = d.pop("detail")

        def _parse_artifact_key(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        artifact_key = _parse_artifact_key(d.pop("artifact_key", UNSET))


        def _parse_free_bytes(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        free_bytes = _parse_free_bytes(d.pop("free_bytes", UNSET))


        def _parse_log_excerpt(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        log_excerpt = _parse_log_excerpt(d.pop("log_excerpt", UNSET))


        _recovery_actions = d.pop("recovery_actions", UNSET)
        recovery_actions: list[AvailabilityRecoveryAction] | Unset = UNSET
        if _recovery_actions is not UNSET:
            recovery_actions = []
            for recovery_actions_item_data in _recovery_actions:
                recovery_actions_item = check_availability_recovery_action(recovery_actions_item_data)



                recovery_actions.append(recovery_actions_item)


        def _parse_required_bytes(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        required_bytes = _parse_required_bytes(d.pop("required_bytes", UNSET))


        def _parse_retry_after_seconds(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        retry_after_seconds = _parse_retry_after_seconds(d.pop("retry_after_seconds", UNSET))


        def _parse_retry_time(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        retry_time = _parse_retry_time(d.pop("retry_time", UNSET))


        retryable = d.pop("retryable", UNSET)

        def _parse_shortfall_bytes(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        shortfall_bytes = _parse_shortfall_bytes(d.pop("shortfall_bytes", UNSET))


        availability_operation_failure = cls(
            code=code,
            detail=detail,
            artifact_key=artifact_key,
            free_bytes=free_bytes,
            log_excerpt=log_excerpt,
            recovery_actions=recovery_actions,
            required_bytes=required_bytes,
            retry_after_seconds=retry_after_seconds,
            retry_time=retry_time,
            retryable=retryable,
            shortfall_bytes=shortfall_bytes,
        )

        return availability_operation_failure
