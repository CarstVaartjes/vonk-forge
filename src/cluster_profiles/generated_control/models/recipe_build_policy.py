from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast

if TYPE_CHECKING:
  from ..models.recipe_build_policy_finding import RecipeBuildPolicyFinding





T = TypeVar("T", bound="RecipeBuildPolicy")



@_attrs_define
class RecipeBuildPolicy:
    """
        Attributes:
            dockerfile (str):
            findings (list['RecipeBuildPolicyFinding']):
            passed (bool):
     """

    dockerfile: str
    findings: list['RecipeBuildPolicyFinding']
    passed: bool





    def to_dict(self) -> dict[str, Any]:
        from ..models.recipe_build_policy_finding import RecipeBuildPolicyFinding
        dockerfile = self.dockerfile

        findings = []
        for findings_item_data in self.findings:
            findings_item = findings_item_data.to_dict()
            findings.append(findings_item)



        passed = self.passed


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "dockerfile": dockerfile,
            "findings": findings,
            "passed": passed,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.recipe_build_policy_finding import RecipeBuildPolicyFinding
        d = dict(src_dict)
        dockerfile = d.pop("dockerfile")

        findings = []
        _findings = d.pop("findings")
        for findings_item_data in (_findings):
            findings_item = RecipeBuildPolicyFinding.from_dict(findings_item_data)



            findings.append(findings_item)


        passed = d.pop("passed")

        recipe_build_policy = cls(
            dockerfile=dockerfile,
            findings=findings,
            passed=passed,
        )

        return recipe_build_policy
