from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast

if TYPE_CHECKING:
  from ..models.runtime_preflight_finding import RuntimePreflightFinding





T = TypeVar("T", bound="RuntimePreflightResult")



@_attrs_define
class RuntimePreflightResult:
    """
        Attributes:
            findings (list[RuntimePreflightFinding]):
            fingerprint (str):
            observed_at (int):
     """

    findings: list[RuntimePreflightFinding]
    fingerprint: str
    observed_at: int





    def to_dict(self) -> dict[str, Any]:
        from ..models.runtime_preflight_finding import RuntimePreflightFinding # noqa: PLC0415
        findings = []
        for findings_item_data in self.findings:
            findings_item = findings_item_data.to_dict()
            findings.append(findings_item)



        fingerprint = self.fingerprint

        observed_at = self.observed_at


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "findings": findings,
            "fingerprint": fingerprint,
            "observed_at": observed_at,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.runtime_preflight_finding import RuntimePreflightFinding # noqa: PLC0415
        d = dict(src_dict)
        findings = []
        _findings = d.pop("findings")
        for findings_item_data in (_findings):
            findings_item = RuntimePreflightFinding.from_dict(findings_item_data)



            findings.append(findings_item)


        fingerprint = d.pop("fingerprint")

        observed_at = d.pop("observed_at")

        runtime_preflight_result = cls(
            findings=findings,
            fingerprint=fingerprint,
            observed_at=observed_at,
        )

        return runtime_preflight_result
