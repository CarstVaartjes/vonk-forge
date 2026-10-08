from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.distribution_code import check_distribution_code
from ..models.distribution_code import DistributionCode
from ..models.wait_reason import check_wait_reason
from ..models.wait_reason import WaitReason
from typing import cast
from typing import Literal, cast






T = TypeVar("T", bound="RunSwitchDistributionEndedResult")



@_attrs_define
class RunSwitchDistributionEndedResult:
    """ A missing durable distribution child; no node effect is asserted.

        Attributes:
            error_code (DistributionCode): Why a distribution assignment object cannot be served to a Spark.
            phase (Literal['transfer']):
            reason (WaitReason): Typed reason codes for an effect that cannot be confirmed (the *unknown* kind).

                Each code names the one fact the executor could not establish.  The free
                text of a report is for people; the Controller decides on this code.
            subphase (Literal['target-copy']):
     """

    error_code: DistributionCode
    phase: Literal['transfer']
    reason: WaitReason
    subphase: Literal['target-copy']





    def to_dict(self) -> dict[str, Any]:
        error_code: str = self.error_code

        phase = self.phase

        reason: str = self.reason

        subphase = self.subphase


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "error_code": error_code,
            "phase": phase,
            "reason": reason,
            "subphase": subphase,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        error_code = check_distribution_code(d.pop("error_code"))




        phase = cast(Literal['transfer'] , d.pop("phase"))
        if phase != 'transfer':
            raise ValueError(f"phase must match const 'transfer', got '{phase}'")

        reason = check_wait_reason(d.pop("reason"))




        subphase = cast(Literal['target-copy'] , d.pop("subphase"))
        if subphase != 'target-copy':
            raise ValueError(f"subphase must match const 'target-copy', got '{subphase}'")

        run_switch_distribution_ended_result = cls(
            error_code=error_code,
            phase=phase,
            reason=reason,
            subphase=subphase,
        )

        return run_switch_distribution_ended_result
