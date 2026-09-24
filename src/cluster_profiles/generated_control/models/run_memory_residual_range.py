from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.run_memory_residual_range_reservation_kind import check_run_memory_residual_range_reservation_kind
from ..models.run_memory_residual_range_reservation_kind import RunMemoryResidualRangeReservationKind
from ..types import UNSET, Unset
from typing import cast
from typing import Literal, Union, cast






T = TypeVar("T", bound="RunMemoryResidualRange")



@_attrs_define
class RunMemoryResidualRange:
    """ Possible remaining bytes for one exact active run reservation.

        Attributes:
            maximum_bytes (int):
            reservation_kind (RunMemoryResidualRangeReservationKind):
            run_generation (int):
            run_id (str):
            minimum_bytes (Union[Literal[0], Unset]):  Default: 0.
     """

    maximum_bytes: int
    reservation_kind: RunMemoryResidualRangeReservationKind
    run_generation: int
    run_id: str
    minimum_bytes: Union[Literal[0], Unset] = 0





    def to_dict(self) -> dict[str, Any]:
        maximum_bytes = self.maximum_bytes

        reservation_kind: str = self.reservation_kind

        run_generation = self.run_generation

        run_id = self.run_id

        minimum_bytes = self.minimum_bytes


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "maximum_bytes": maximum_bytes,
            "reservation_kind": reservation_kind,
            "run_generation": run_generation,
            "run_id": run_id,
        })
        if minimum_bytes is not UNSET:
            field_dict["minimum_bytes"] = minimum_bytes

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        maximum_bytes = d.pop("maximum_bytes")

        reservation_kind = check_run_memory_residual_range_reservation_kind(d.pop("reservation_kind"))




        run_generation = d.pop("run_generation")

        run_id = d.pop("run_id")

        minimum_bytes = cast(Union[Literal[0], Unset] , d.pop("minimum_bytes", UNSET))
        if minimum_bytes != 0 and not isinstance(minimum_bytes, Unset):
            raise ValueError(f"minimum_bytes must match const 0, got '{minimum_bytes}'")

        run_memory_residual_range = cls(
            maximum_bytes=maximum_bytes,
            reservation_kind=reservation_kind,
            run_generation=run_generation,
            run_id=run_id,
            minimum_bytes=minimum_bytes,
        )

        return run_memory_residual_range
