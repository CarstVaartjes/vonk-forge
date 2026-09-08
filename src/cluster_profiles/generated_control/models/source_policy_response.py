from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast

if TYPE_CHECKING:
  from ..models.stored_policy_finding import StoredPolicyFinding





T = TypeVar("T", bound="SourcePolicyResponse")



@_attrs_define
class SourcePolicyResponse:
    """
        Attributes:
            dockerfile (str):
            findings (list['StoredPolicyFinding']):
            passed (bool):
            source_bundle_sha256 (str):
     """

    dockerfile: str
    findings: list['StoredPolicyFinding']
    passed: bool
    source_bundle_sha256: str





    def to_dict(self) -> dict[str, Any]:
        from ..models.stored_policy_finding import StoredPolicyFinding
        dockerfile = self.dockerfile

        findings = []
        for findings_item_data in self.findings:
            findings_item = findings_item_data.to_dict()
            findings.append(findings_item)



        passed = self.passed

        source_bundle_sha256 = self.source_bundle_sha256


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "dockerfile": dockerfile,
            "findings": findings,
            "passed": passed,
            "source_bundle_sha256": source_bundle_sha256,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.stored_policy_finding import StoredPolicyFinding
        d = dict(src_dict)
        dockerfile = d.pop("dockerfile")

        findings = []
        _findings = d.pop("findings")
        for findings_item_data in (_findings):
            findings_item = StoredPolicyFinding.from_dict(findings_item_data)



            findings.append(findings_item)


        passed = d.pop("passed")

        source_bundle_sha256 = d.pop("source_bundle_sha256")

        source_policy_response = cls(
            dockerfile=dockerfile,
            findings=findings,
            passed=passed,
            source_bundle_sha256=source_bundle_sha256,
        )

        return source_policy_response
