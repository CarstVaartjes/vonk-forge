from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
from typing import Union






T = TypeVar("T", bound="CacheRemovalBlocker")



@_attrs_define
class CacheRemovalBlocker:
    """
        Attributes:
            code (str):
            detail (str):
            retryable (bool):
            recovery_actions (Union[Unset, list[str]]):
     """

    code: str
    detail: str
    retryable: bool
    recovery_actions: Union[Unset, list[str]] = UNSET





    def to_dict(self) -> dict[str, Any]:
        code = self.code

        detail = self.detail

        retryable = self.retryable

        recovery_actions: Union[Unset, list[str]] = UNSET
        if not isinstance(self.recovery_actions, Unset):
            recovery_actions = self.recovery_actions




        field_dict: dict[str, Any] = {}

        field_dict.update({
            "code": code,
            "detail": detail,
            "retryable": retryable,
        })
        if recovery_actions is not UNSET:
            field_dict["recovery_actions"] = recovery_actions

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        code = d.pop("code")

        detail = d.pop("detail")

        retryable = d.pop("retryable")

        recovery_actions = cast(list[str], d.pop("recovery_actions", UNSET))


        cache_removal_blocker = cls(
            code=code,
            detail=detail,
            retryable=retryable,
            recovery_actions=recovery_actions,
        )

        return cache_removal_blocker
