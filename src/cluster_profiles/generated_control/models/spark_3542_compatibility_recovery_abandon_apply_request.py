from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import Literal, cast






T = TypeVar("T", bound="Spark3542CompatibilityRecoveryAbandonApplyRequest")



@_attrs_define
class Spark3542CompatibilityRecoveryAbandonApplyRequest:
    """
        Attributes:
            confirmation (Literal['abandon-expired-spark3542-a122-recovery']):
            plan_digest (str):
     """

    confirmation: Literal['abandon-expired-spark3542-a122-recovery']
    plan_digest: str





    def to_dict(self) -> dict[str, Any]:
        confirmation = self.confirmation

        plan_digest = self.plan_digest


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "confirmation": confirmation,
            "plan_digest": plan_digest,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        confirmation = cast(Literal['abandon-expired-spark3542-a122-recovery'] , d.pop("confirmation"))
        if confirmation != 'abandon-expired-spark3542-a122-recovery':
            raise ValueError(f"confirmation must match const 'abandon-expired-spark3542-a122-recovery', got '{confirmation}'")

        plan_digest = d.pop("plan_digest")

        spark_3542_compatibility_recovery_abandon_apply_request = cls(
            confirmation=confirmation,
            plan_digest=plan_digest,
        )

        return spark_3542_compatibility_recovery_abandon_apply_request
