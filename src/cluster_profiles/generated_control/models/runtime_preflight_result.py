from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast
from typing import Literal, cast

if TYPE_CHECKING:
  from ..models.runtime_preflight_finding import RuntimePreflightFinding





T = TypeVar("T", bound="RuntimePreflightResult")



@_attrs_define
class RuntimePreflightResult:
    """
        Attributes:
            cached (bool):
            duration_ms (int):
            findings (list['RuntimePreflightFinding']):
            fingerprint (str):
            observed_at (int):
            request_sha256 (str):
            schema_version (Literal[1]):
     """

    cached: bool
    duration_ms: int
    findings: list['RuntimePreflightFinding']
    fingerprint: str
    observed_at: int
    request_sha256: str
    schema_version: Literal[1]





    def to_dict(self) -> dict[str, Any]:
        from ..models.runtime_preflight_finding import RuntimePreflightFinding
        cached = self.cached

        duration_ms = self.duration_ms

        findings = []
        for findings_item_data in self.findings:
            findings_item = findings_item_data.to_dict()
            findings.append(findings_item)



        fingerprint = self.fingerprint

        observed_at = self.observed_at

        request_sha256 = self.request_sha256

        schema_version = self.schema_version


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "cached": cached,
            "duration_ms": duration_ms,
            "findings": findings,
            "fingerprint": fingerprint,
            "observed_at": observed_at,
            "request_sha256": request_sha256,
            "schema_version": schema_version,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.runtime_preflight_finding import RuntimePreflightFinding
        d = dict(src_dict)
        cached = d.pop("cached")

        duration_ms = d.pop("duration_ms")

        findings = []
        _findings = d.pop("findings")
        for findings_item_data in (_findings):
            findings_item = RuntimePreflightFinding.from_dict(findings_item_data)



            findings.append(findings_item)


        fingerprint = d.pop("fingerprint")

        observed_at = d.pop("observed_at")

        request_sha256 = d.pop("request_sha256")

        schema_version = cast(Literal[1] , d.pop("schema_version"))
        if schema_version != 1:
            raise ValueError(f"schema_version must match const 1, got '{schema_version}'")

        runtime_preflight_result = cls(
            cached=cached,
            duration_ms=duration_ms,
            findings=findings,
            fingerprint=fingerprint,
            observed_at=observed_at,
            request_sha256=request_sha256,
            schema_version=schema_version,
        )

        return runtime_preflight_result
