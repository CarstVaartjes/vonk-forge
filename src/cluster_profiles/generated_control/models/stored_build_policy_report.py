from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast

if TYPE_CHECKING:
  from ..models.stored_policy_finding import StoredPolicyFinding
  from ..models.stored_prebuilt_decision import StoredPrebuiltDecision





T = TypeVar("T", bound="StoredBuildPolicyReport")



@_attrs_define
class StoredBuildPolicyReport:
    """
        Attributes:
            artifact_format (str):
            dockerfile (str):
            findings (list[StoredPolicyFinding]):
            passed (bool):
            source_bundle_sha256 (str):
            builder_binary_digest (None | str | Unset):
            prebuilt_decision (None | StoredPrebuiltDecision | Unset):
            prebuilt_image (None | str | Unset):
     """

    artifact_format: str
    dockerfile: str
    findings: list[StoredPolicyFinding]
    passed: bool
    source_bundle_sha256: str
    builder_binary_digest: None | str | Unset = UNSET
    prebuilt_decision: None | StoredPrebuiltDecision | Unset = UNSET
    prebuilt_image: None | str | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.stored_policy_finding import StoredPolicyFinding # noqa: PLC0415
        from ..models.stored_prebuilt_decision import StoredPrebuiltDecision # noqa: PLC0415
        artifact_format = self.artifact_format

        dockerfile = self.dockerfile

        findings = []
        for findings_item_data in self.findings:
            findings_item = findings_item_data.to_dict()
            findings.append(findings_item)



        passed = self.passed

        source_bundle_sha256 = self.source_bundle_sha256

        builder_binary_digest: None | str | Unset
        if isinstance(self.builder_binary_digest, Unset):
            builder_binary_digest = UNSET
        else:
            builder_binary_digest = self.builder_binary_digest

        prebuilt_decision: dict[str, Any] | None | Unset
        if isinstance(self.prebuilt_decision, Unset):
            prebuilt_decision = UNSET
        elif isinstance(self.prebuilt_decision, StoredPrebuiltDecision):
            prebuilt_decision = self.prebuilt_decision.to_dict()
        else:
            prebuilt_decision = self.prebuilt_decision

        prebuilt_image: None | str | Unset
        if isinstance(self.prebuilt_image, Unset):
            prebuilt_image = UNSET
        else:
            prebuilt_image = self.prebuilt_image


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "artifact_format": artifact_format,
            "dockerfile": dockerfile,
            "findings": findings,
            "passed": passed,
            "source_bundle_sha256": source_bundle_sha256,
        })
        if builder_binary_digest is not UNSET:
            field_dict["builder_binary_digest"] = builder_binary_digest
        if prebuilt_decision is not UNSET:
            field_dict["prebuilt_decision"] = prebuilt_decision
        if prebuilt_image is not UNSET:
            field_dict["prebuilt_image"] = prebuilt_image

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.stored_policy_finding import StoredPolicyFinding # noqa: PLC0415
        from ..models.stored_prebuilt_decision import StoredPrebuiltDecision # noqa: PLC0415
        d = dict(src_dict)
        artifact_format = d.pop("artifact_format")

        dockerfile = d.pop("dockerfile")

        findings = []
        _findings = d.pop("findings")
        for findings_item_data in (_findings):
            findings_item = StoredPolicyFinding.from_dict(findings_item_data)



            findings.append(findings_item)


        passed = d.pop("passed")

        source_bundle_sha256 = d.pop("source_bundle_sha256")

        def _parse_builder_binary_digest(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        builder_binary_digest = _parse_builder_binary_digest(d.pop("builder_binary_digest", UNSET))


        def _parse_prebuilt_decision(data: object) -> None | StoredPrebuiltDecision | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                prebuilt_decision_type_0 = StoredPrebuiltDecision.from_dict(data)



                return prebuilt_decision_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | StoredPrebuiltDecision | Unset, data)

        prebuilt_decision = _parse_prebuilt_decision(d.pop("prebuilt_decision", UNSET))


        def _parse_prebuilt_image(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        prebuilt_image = _parse_prebuilt_image(d.pop("prebuilt_image", UNSET))


        stored_build_policy_report = cls(
            artifact_format=artifact_format,
            dockerfile=dockerfile,
            findings=findings,
            passed=passed,
            source_bundle_sha256=source_bundle_sha256,
            builder_binary_digest=builder_binary_digest,
            prebuilt_decision=prebuilt_decision,
            prebuilt_image=prebuilt_image,
        )

        return stored_build_policy_report
