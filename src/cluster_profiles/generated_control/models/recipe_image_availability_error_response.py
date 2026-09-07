from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
from typing import Literal, Union, cast

if TYPE_CHECKING:
  from ..models.availability_operation_failure import AvailabilityOperationFailure





T = TypeVar("T", bound="RecipeImageAvailabilityErrorResponse")



@_attrs_define
class RecipeImageAvailabilityErrorResponse:
    """
        Attributes:
            failure (AvailabilityOperationFailure): Shared failure wire contract for model and image availability.
            schema_version (Union[Literal[2], Unset]):  Default: 2.
     """

    failure: 'AvailabilityOperationFailure'
    schema_version: Union[Literal[2], Unset] = 2





    def to_dict(self) -> dict[str, Any]:
        from ..models.availability_operation_failure import AvailabilityOperationFailure
        failure = self.failure.to_dict()

        schema_version = self.schema_version


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "failure": failure,
        })
        if schema_version is not UNSET:
            field_dict["schema_version"] = schema_version

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.availability_operation_failure import AvailabilityOperationFailure
        d = dict(src_dict)
        failure = AvailabilityOperationFailure.from_dict(d.pop("failure"))




        schema_version = cast(Union[Literal[2], Unset] , d.pop("schema_version", UNSET))
        if schema_version != 2 and not isinstance(schema_version, Unset):
            raise ValueError(f"schema_version must match const 2, got '{schema_version}'")

        recipe_image_availability_error_response = cls(
            failure=failure,
            schema_version=schema_version,
        )

        return recipe_image_availability_error_response
