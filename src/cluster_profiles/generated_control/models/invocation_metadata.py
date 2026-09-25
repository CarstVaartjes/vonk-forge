from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Union

if TYPE_CHECKING:
  from ..models.invocation_metadata_context import InvocationMetadataContext





T = TypeVar("T", bound="InvocationMetadata")



@_attrs_define
class InvocationMetadata:
    """ Context for audit and tracing which has no decision-making authority.

        Attributes:
            context (Union[Unset, InvocationMetadataContext]):
            correlation_id (Union[None, Unset, str]):
            origin (Union[Unset, str]):  Default: 'operator'.
            reason (Union[None, Unset, str]):
     """

    context: Union[Unset, 'InvocationMetadataContext'] = UNSET
    correlation_id: Union[None, Unset, str] = UNSET
    origin: Union[Unset, str] = 'operator'
    reason: Union[None, Unset, str] = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.invocation_metadata_context import InvocationMetadataContext
        context: Union[Unset, dict[str, Any]] = UNSET
        if not isinstance(self.context, Unset):
            context = self.context.to_dict()

        correlation_id: Union[None, Unset, str]
        if isinstance(self.correlation_id, Unset):
            correlation_id = UNSET
        else:
            correlation_id = self.correlation_id

        origin = self.origin

        reason: Union[None, Unset, str]
        if isinstance(self.reason, Unset):
            reason = UNSET
        else:
            reason = self.reason


        field_dict: dict[str, Any] = {}

        field_dict.update({
        })
        if context is not UNSET:
            field_dict["context"] = context
        if correlation_id is not UNSET:
            field_dict["correlation_id"] = correlation_id
        if origin is not UNSET:
            field_dict["origin"] = origin
        if reason is not UNSET:
            field_dict["reason"] = reason

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.invocation_metadata_context import InvocationMetadataContext
        d = dict(src_dict)
        _context = d.pop("context", UNSET)
        context: Union[Unset, InvocationMetadataContext]
        if isinstance(_context,  Unset):
            context = UNSET
        else:
            context = InvocationMetadataContext.from_dict(_context)




        def _parse_correlation_id(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        correlation_id = _parse_correlation_id(d.pop("correlation_id", UNSET))


        origin = d.pop("origin", UNSET)

        def _parse_reason(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        reason = _parse_reason(d.pop("reason", UNSET))


        invocation_metadata = cls(
            context=context,
            correlation_id=correlation_id,
            origin=origin,
            reason=reason,
        )

        return invocation_metadata
