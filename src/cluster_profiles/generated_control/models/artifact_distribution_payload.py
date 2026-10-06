from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="ArtifactDistributionPayload")



@_attrs_define
class ArtifactDistributionPayload:
    """ The complete payload accepted by the artifact transfer operation.

        Attributes:
            plan_digest (str):
     """

    plan_digest: str





    def to_dict(self) -> dict[str, Any]:
        plan_digest = self.plan_digest


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "plan_digest": plan_digest,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        plan_digest = d.pop("plan_digest")

        artifact_distribution_payload = cls(
            plan_digest=plan_digest,
        )

        return artifact_distribution_payload
