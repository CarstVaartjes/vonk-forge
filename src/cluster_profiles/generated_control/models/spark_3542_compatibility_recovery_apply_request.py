from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import Literal, cast






T = TypeVar("T", bound="Spark3542CompatibilityRecoveryApplyRequest")



@_attrs_define
class Spark3542CompatibilityRecoveryApplyRequest:
    """
        Attributes:
            confirmation (Literal['restart-staged-a122-recovery-on-spark3542']):
            plan_digest (str):
     """

    confirmation: Literal['restart-staged-a122-recovery-on-spark3542']
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
        confirmation = cast(Literal['restart-staged-a122-recovery-on-spark3542'] , d.pop("confirmation"))
        if confirmation != 'restart-staged-a122-recovery-on-spark3542':
            raise ValueError(f"confirmation must match const 'restart-staged-a122-recovery-on-spark3542', got '{confirmation}'")

        plan_digest = d.pop("plan_digest")

        spark_3542_compatibility_recovery_apply_request = cls(
            confirmation=confirmation,
            plan_digest=plan_digest,
        )

        return spark_3542_compatibility_recovery_apply_request
