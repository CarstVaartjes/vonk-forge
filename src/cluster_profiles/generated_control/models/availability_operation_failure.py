from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.availability_recovery_action import AvailabilityRecoveryAction
from ..models.availability_recovery_action import check_availability_recovery_action
from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Union






T = TypeVar("T", bound="AvailabilityOperationFailure")



@_attrs_define
class AvailabilityOperationFailure:
    """ Shared failure wire contract for model and image availability.

        Attributes:
            code (str):
            detail (str):
            free_bytes (Union[None, Unset, int]):
            log_excerpt (Union[None, Unset, str]):
            recovery_actions (Union[Unset, list[AvailabilityRecoveryAction]]):
            required_bytes (Union[None, Unset, int]):
            retry_after_seconds (Union[None, Unset, int]):
            retry_time (Union[None, Unset, str]):
            retryable (Union[Unset, bool]):  Default: False.
            shortfall_bytes (Union[None, Unset, int]):
     """

    code: str
    detail: str
    free_bytes: Union[None, Unset, int] = UNSET
    log_excerpt: Union[None, Unset, str] = UNSET
    recovery_actions: Union[Unset, list[AvailabilityRecoveryAction]] = UNSET
    required_bytes: Union[None, Unset, int] = UNSET
    retry_after_seconds: Union[None, Unset, int] = UNSET
    retry_time: Union[None, Unset, str] = UNSET
    retryable: Union[Unset, bool] = False
    shortfall_bytes: Union[None, Unset, int] = UNSET





    def to_dict(self) -> dict[str, Any]:
        code = self.code

        detail = self.detail

        free_bytes: Union[None, Unset, int]
        if isinstance(self.free_bytes, Unset):
            free_bytes = UNSET
        else:
            free_bytes = self.free_bytes

        log_excerpt: Union[None, Unset, str]
        if isinstance(self.log_excerpt, Unset):
            log_excerpt = UNSET
        else:
            log_excerpt = self.log_excerpt

        recovery_actions: Union[Unset, list[str]] = UNSET
        if not isinstance(self.recovery_actions, Unset):
            recovery_actions = []
            for recovery_actions_item_data in self.recovery_actions:
                recovery_actions_item: str = recovery_actions_item_data
                recovery_actions.append(recovery_actions_item)



        required_bytes: Union[None, Unset, int]
        if isinstance(self.required_bytes, Unset):
            required_bytes = UNSET
        else:
            required_bytes = self.required_bytes

        retry_after_seconds: Union[None, Unset, int]
        if isinstance(self.retry_after_seconds, Unset):
            retry_after_seconds = UNSET
        else:
            retry_after_seconds = self.retry_after_seconds

        retry_time: Union[None, Unset, str]
        if isinstance(self.retry_time, Unset):
            retry_time = UNSET
        else:
            retry_time = self.retry_time

        retryable = self.retryable

        shortfall_bytes: Union[None, Unset, int]
        if isinstance(self.shortfall_bytes, Unset):
            shortfall_bytes = UNSET
        else:
            shortfall_bytes = self.shortfall_bytes


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "code": code,
            "detail": detail,
        })
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

        def _parse_free_bytes(data: object) -> Union[None, Unset, int]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, int], data)

        free_bytes = _parse_free_bytes(d.pop("free_bytes", UNSET))


        def _parse_log_excerpt(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        log_excerpt = _parse_log_excerpt(d.pop("log_excerpt", UNSET))


        recovery_actions = []
        _recovery_actions = d.pop("recovery_actions", UNSET)
        for recovery_actions_item_data in (_recovery_actions or []):
            recovery_actions_item = check_availability_recovery_action(recovery_actions_item_data)



            recovery_actions.append(recovery_actions_item)


        def _parse_required_bytes(data: object) -> Union[None, Unset, int]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, int], data)

        required_bytes = _parse_required_bytes(d.pop("required_bytes", UNSET))


        def _parse_retry_after_seconds(data: object) -> Union[None, Unset, int]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, int], data)

        retry_after_seconds = _parse_retry_after_seconds(d.pop("retry_after_seconds", UNSET))


        def _parse_retry_time(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        retry_time = _parse_retry_time(d.pop("retry_time", UNSET))


        retryable = d.pop("retryable", UNSET)

        def _parse_shortfall_bytes(data: object) -> Union[None, Unset, int]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, int], data)

        shortfall_bytes = _parse_shortfall_bytes(d.pop("shortfall_bytes", UNSET))


        availability_operation_failure = cls(
            code=code,
            detail=detail,
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
