from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast

if TYPE_CHECKING:
  from ..models.invalid_request import InvalidRequest
  from ..models.security_refusal import SecurityRefusal
  from ..models.unknown_error import UnknownError





T = TypeVar("T", bound="ErrorCatalog")



@_attrs_define
class ErrorCatalog:
    """ Carrier that publishes the error-category union into every generated surface.

    Never sent: the wire schema, OpenAPI and the TypeScript client all emit the
    tagged union as one ``OperationError``.

        Attributes:
            error (InvalidRequest | SecurityRefusal | UnknownError):
     """

    error: InvalidRequest | SecurityRefusal | UnknownError





    def to_dict(self) -> dict[str, Any]:
        from ..models.invalid_request import InvalidRequest # noqa: PLC0415
        from ..models.security_refusal import SecurityRefusal # noqa: PLC0415
        from ..models.unknown_error import UnknownError # noqa: PLC0415
        error: dict[str, Any]
        if isinstance(self.error, SecurityRefusal):
            error = self.error.to_dict()
        elif isinstance(self.error, InvalidRequest):
            error = self.error.to_dict()
        else:
            error = self.error.to_dict()



        field_dict: dict[str, Any] = {}

        field_dict.update({
            "error": error,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.invalid_request import InvalidRequest # noqa: PLC0415
        from ..models.security_refusal import SecurityRefusal # noqa: PLC0415
        from ..models.unknown_error import UnknownError # noqa: PLC0415
        d = dict(src_dict)
        def _parse_error(data: object) -> InvalidRequest | SecurityRefusal | UnknownError:
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                error_type_0 = SecurityRefusal.from_dict(data)



                return error_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                error_type_1 = InvalidRequest.from_dict(data)



                return error_type_1
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            if not isinstance(data, dict):
                raise TypeError()
            error_type_2 = UnknownError.from_dict(data)



            return error_type_2

        error = _parse_error(d.pop("error"))


        error_catalog = cls(
            error=error,
        )

        return error_catalog
