from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
from typing import Literal, cast

if TYPE_CHECKING:
  from ..models.availability_operation_failure import AvailabilityOperationFailure





T = TypeVar("T", bound="RecipeCacheRemovalCheckpoint")



@_attrs_define
class RecipeCacheRemovalCheckpoint:
    """ Durable resumable progress for a recipe image and model-cache removal.

        Attributes:
            failure (AvailabilityOperationFailure | None):
            image_index (int):
            image_pending_bytes (int | None):
            image_reclaimed_bytes (int):
            model_index (int):
            model_reclaimed_bytes (int):
            retry_attempts (int):
            schema_version (Literal[2]):
            scope_pending (bool | Unset):  Default: False.
     """

    failure: AvailabilityOperationFailure | None
    image_index: int
    image_pending_bytes: int | None
    image_reclaimed_bytes: int
    model_index: int
    model_reclaimed_bytes: int
    retry_attempts: int
    schema_version: Literal[2]
    scope_pending: bool | Unset = False





    def to_dict(self) -> dict[str, Any]:
        from ..models.availability_operation_failure import AvailabilityOperationFailure # noqa: PLC0415
        failure: dict[str, Any] | None
        if isinstance(self.failure, AvailabilityOperationFailure):
            failure = self.failure.to_dict()
        else:
            failure = self.failure

        image_index = self.image_index

        image_pending_bytes: int | None
        image_pending_bytes = self.image_pending_bytes

        image_reclaimed_bytes = self.image_reclaimed_bytes

        model_index = self.model_index

        model_reclaimed_bytes = self.model_reclaimed_bytes

        retry_attempts = self.retry_attempts

        schema_version = self.schema_version

        scope_pending = self.scope_pending


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "failure": failure,
            "image_index": image_index,
            "image_pending_bytes": image_pending_bytes,
            "image_reclaimed_bytes": image_reclaimed_bytes,
            "model_index": model_index,
            "model_reclaimed_bytes": model_reclaimed_bytes,
            "retry_attempts": retry_attempts,
            "schema_version": schema_version,
        })
        if scope_pending is not UNSET:
            field_dict["scope_pending"] = scope_pending

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.availability_operation_failure import AvailabilityOperationFailure # noqa: PLC0415
        d = dict(src_dict)
        def _parse_failure(data: object) -> AvailabilityOperationFailure | None:
            if data is None:
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                failure_type_0 = AvailabilityOperationFailure.from_dict(data)



                return failure_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(AvailabilityOperationFailure | None, data)

        failure = _parse_failure(d.pop("failure"))


        image_index = d.pop("image_index")

        def _parse_image_pending_bytes(data: object) -> int | None:
            if data is None:
                return data
            return cast(int | None, data)

        image_pending_bytes = _parse_image_pending_bytes(d.pop("image_pending_bytes"))


        image_reclaimed_bytes = d.pop("image_reclaimed_bytes")

        model_index = d.pop("model_index")

        model_reclaimed_bytes = d.pop("model_reclaimed_bytes")

        retry_attempts = d.pop("retry_attempts")

        schema_version = cast(Literal[2] , d.pop("schema_version"))
        if schema_version != 2:
            raise ValueError(f"schema_version must match const 2, got '{schema_version}'")

        scope_pending = d.pop("scope_pending", UNSET)

        recipe_cache_removal_checkpoint = cls(
            failure=failure,
            image_index=image_index,
            image_pending_bytes=image_pending_bytes,
            image_reclaimed_bytes=image_reclaimed_bytes,
            model_index=model_index,
            model_reclaimed_bytes=model_reclaimed_bytes,
            retry_attempts=retry_attempts,
            schema_version=schema_version,
            scope_pending=scope_pending,
        )

        return recipe_cache_removal_checkpoint
