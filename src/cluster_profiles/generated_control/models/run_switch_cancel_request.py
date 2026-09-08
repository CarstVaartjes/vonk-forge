from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import Literal, Union, cast






T = TypeVar("T", bound="RunSwitchCancelRequest")



@_attrs_define
class RunSwitchCancelRequest:
    """
        Attributes:
            reason (str):
            request_key (str):
            schema_version (Union[Literal[2], Unset]):  Default: 2.
     """

    reason: str
    request_key: str
    schema_version: Union[Literal[2], Unset] = 2





    def to_dict(self) -> dict[str, Any]:
        reason = self.reason

        request_key = self.request_key

        schema_version = self.schema_version


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "reason": reason,
            "request_key": request_key,
        })
        if schema_version is not UNSET:
            field_dict["schema_version"] = schema_version

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        reason = d.pop("reason")

        request_key = d.pop("request_key")

        schema_version = cast(Union[Literal[2], Unset] , d.pop("schema_version", UNSET))
        if schema_version != 2 and not isinstance(schema_version, Unset):
            raise ValueError(f"schema_version must match const 2, got '{schema_version}'")

        run_switch_cancel_request = cls(
            reason=reason,
            request_key=request_key,
            schema_version=schema_version,
        )

        return run_switch_cancel_request
