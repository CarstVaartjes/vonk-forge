from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.lifecycle_state import check_lifecycle_state
from ..models.lifecycle_state import LifecycleState
from ..models.wait_reason import check_wait_reason
from ..models.wait_reason import WaitReason
from typing import cast
from typing import Literal, cast






T = TypeVar("T", bound="EnrollmentObservationOutcome")



@_attrs_define
class EnrollmentObservationOutcome:
    """
        Attributes:
            category (Literal['unknown']):
            reason (WaitReason): Typed reason codes for an effect that cannot be confirmed (the *unknown* kind).

                Each code names the one fact the executor could not establish.  The free
                text of a report is for people; the Controller decides on this code.
            state (LifecycleState): The nine lifecycle states: the one vocabulary a stored ``state`` speaks.

                ``superseded`` is a definite, non-failed end: a newer request replaced the
                work, so nothing is left for anyone to do.  The legacy spellings
                (``waiting``, ``partial``, ``cancelling``, ``expired``,
                ``waiting-for-operator``) are *aliases*: see :data:`STATE_ALIASES`, the one
                table that says what each of them means, per subject.
     """

    category: Literal['unknown']
    reason: WaitReason
    state: LifecycleState





    def to_dict(self) -> dict[str, Any]:
        category = self.category

        reason: str = self.reason

        state: str = self.state


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "category": category,
            "reason": reason,
            "state": state,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        category = cast(Literal['unknown'] , d.pop("category"))
        if category != 'unknown':
            raise ValueError(f"category must match const 'unknown', got '{category}'")

        reason = check_wait_reason(d.pop("reason"))




        state = check_lifecycle_state(d.pop("state"))




        enrollment_observation_outcome = cls(
            category=category,
            reason=reason,
            state=state,
        )

        return enrollment_observation_outcome
