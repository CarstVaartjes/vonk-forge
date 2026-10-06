from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
from uuid import UUID






T = TypeVar("T", bound="RecipeBuildDependency")



@_attrs_define
class RecipeBuildDependency:
    """ A request persisted before dispatch, then its exact lifecycle child.

        Attributes:
            request_key (UUID):
            operation_id (None | Unset | UUID):
     """

    request_key: UUID
    operation_id: None | Unset | UUID = UNSET





    def to_dict(self) -> dict[str, Any]:
        request_key = str(self.request_key)

        operation_id: None | str | Unset
        if isinstance(self.operation_id, Unset):
            operation_id = UNSET
        elif isinstance(self.operation_id, UUID):
            operation_id = str(self.operation_id)
        else:
            operation_id = self.operation_id


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "request_key": request_key,
        })
        if operation_id is not UNSET:
            field_dict["operation_id"] = operation_id

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        request_key = UUID(d.pop("request_key"))




        def _parse_operation_id(data: object) -> None | Unset | UUID:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                operation_id_type_0 = UUID(data)



                return operation_id_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | Unset | UUID, data)

        operation_id = _parse_operation_id(d.pop("operation_id", UNSET))


        recipe_build_dependency = cls(
            request_key=request_key,
            operation_id=operation_id,
        )

        return recipe_build_dependency
