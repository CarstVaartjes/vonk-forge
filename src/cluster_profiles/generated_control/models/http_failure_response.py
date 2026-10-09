from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast

if TYPE_CHECKING:
  from ..models.http_refusal import HttpRefusal
  from ..models.http_transient import HttpTransient





T = TypeVar("T", bound="HttpFailureResponse")



@_attrs_define
class HttpFailureResponse:
    """
        Attributes:
            failure (HttpRefusal | HttpTransient):
     """

    failure: HttpRefusal | HttpTransient





    def to_dict(self) -> dict[str, Any]:
        from ..models.http_refusal import HttpRefusal # noqa: PLC0415
        from ..models.http_transient import HttpTransient # noqa: PLC0415
        failure: dict[str, Any]
        if isinstance(self.failure, HttpTransient):
            failure = self.failure.to_dict()
        else:
            failure = self.failure.to_dict()



        field_dict: dict[str, Any] = {}

        field_dict.update({
            "failure": failure,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.http_refusal import HttpRefusal # noqa: PLC0415
        from ..models.http_transient import HttpTransient # noqa: PLC0415
        d = dict(src_dict)
        def _parse_failure(data: object) -> HttpRefusal | HttpTransient:
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                failure_type_0 = HttpTransient.from_dict(data)



                return failure_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            if not isinstance(data, dict):
                raise TypeError()
            failure_type_1 = HttpRefusal.from_dict(data)



            return failure_type_1

        failure = _parse_failure(d.pop("failure"))


        http_failure_response = cls(
            failure=failure,
        )

        return http_failure_response
