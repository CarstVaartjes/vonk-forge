from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.build_source_evidence_state import BuildSourceEvidenceState
from ..models.build_source_evidence_state import check_build_source_evidence_state
from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Union






T = TypeVar("T", bound="BuildSourceEvidence")



@_attrs_define
class BuildSourceEvidence:
    """
        Attributes:
            state (BuildSourceEvidenceState):
            detail (Union[None, Unset, str]):
            source_bundle_sha256 (Union[None, Unset, str]):
     """

    state: BuildSourceEvidenceState
    detail: Union[None, Unset, str] = UNSET
    source_bundle_sha256: Union[None, Unset, str] = UNSET





    def to_dict(self) -> dict[str, Any]:
        state: str = self.state

        detail: Union[None, Unset, str]
        if isinstance(self.detail, Unset):
            detail = UNSET
        else:
            detail = self.detail

        source_bundle_sha256: Union[None, Unset, str]
        if isinstance(self.source_bundle_sha256, Unset):
            source_bundle_sha256 = UNSET
        else:
            source_bundle_sha256 = self.source_bundle_sha256


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "state": state,
        })
        if detail is not UNSET:
            field_dict["detail"] = detail
        if source_bundle_sha256 is not UNSET:
            field_dict["source_bundle_sha256"] = source_bundle_sha256

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        state = check_build_source_evidence_state(d.pop("state"))




        def _parse_detail(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        detail = _parse_detail(d.pop("detail", UNSET))


        def _parse_source_bundle_sha256(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        source_bundle_sha256 = _parse_source_bundle_sha256(d.pop("source_bundle_sha256", UNSET))


        build_source_evidence = cls(
            state=state,
            detail=detail,
            source_bundle_sha256=source_bundle_sha256,
        )

        return build_source_evidence
