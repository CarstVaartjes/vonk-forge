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
  from ..models.error_context_response import ErrorContextResponse





T = TypeVar("T", bound="BoundedErrorResponse")



@_attrs_define
class BoundedErrorResponse:
    """
        Attributes:
            detail (str):
            context (Union['ErrorContextResponse', None, Unset]):
     """

    detail: str
    context: Union['ErrorContextResponse', None, Unset] = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.error_context_response import ErrorContextResponse
        detail = self.detail

        context: Union[None, Unset, dict[str, Any]]
        if isinstance(self.context, Unset):
            context = UNSET
        elif isinstance(self.context, ErrorContextResponse):
            context = self.context.to_dict()
        else:
            context = self.context


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "detail": detail,
        })
        if context is not UNSET:
            field_dict["context"] = context

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.error_context_response import ErrorContextResponse
        d = dict(src_dict)
        detail = d.pop("detail")

        def _parse_context(data: object) -> Union['ErrorContextResponse', None, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                context_type_0 = ErrorContextResponse.from_dict(data)



                return context_type_0
            except: # noqa: E722
                pass
            return cast(Union['ErrorContextResponse', None, Unset], data)

        context = _parse_context(d.pop("context", UNSET))


        bounded_error_response = cls(
            detail=detail,
            context=context,
        )

        return bounded_error_response
