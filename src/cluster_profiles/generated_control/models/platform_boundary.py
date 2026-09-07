from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.platform_boundary_boundary import check_platform_boundary_boundary
from ..models.platform_boundary_boundary import PlatformBoundaryBoundary
from ..models.platform_boundary_state import check_platform_boundary_state
from ..models.platform_boundary_state import PlatformBoundaryState
from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Union

if TYPE_CHECKING:
  from ..models.evidence_age import EvidenceAge





T = TypeVar("T", bound="PlatformBoundary")



@_attrs_define
class PlatformBoundary:
    """
        Attributes:
            boundary (PlatformBoundaryBoundary):
            evidence (EvidenceAge):
            state (PlatformBoundaryState):
            image_digest (Union[None, Unset, str]):
            manifest_sha256 (Union[None, Unset, str]):
            source_commit (Union[None, Unset, str]):
     """

    boundary: PlatformBoundaryBoundary
    evidence: 'EvidenceAge'
    state: PlatformBoundaryState
    image_digest: Union[None, Unset, str] = UNSET
    manifest_sha256: Union[None, Unset, str] = UNSET
    source_commit: Union[None, Unset, str] = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.evidence_age import EvidenceAge
        boundary: str = self.boundary

        evidence = self.evidence.to_dict()

        state: str = self.state

        image_digest: Union[None, Unset, str]
        if isinstance(self.image_digest, Unset):
            image_digest = UNSET
        else:
            image_digest = self.image_digest

        manifest_sha256: Union[None, Unset, str]
        if isinstance(self.manifest_sha256, Unset):
            manifest_sha256 = UNSET
        else:
            manifest_sha256 = self.manifest_sha256

        source_commit: Union[None, Unset, str]
        if isinstance(self.source_commit, Unset):
            source_commit = UNSET
        else:
            source_commit = self.source_commit


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "boundary": boundary,
            "evidence": evidence,
            "state": state,
        })
        if image_digest is not UNSET:
            field_dict["image_digest"] = image_digest
        if manifest_sha256 is not UNSET:
            field_dict["manifest_sha256"] = manifest_sha256
        if source_commit is not UNSET:
            field_dict["source_commit"] = source_commit

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.evidence_age import EvidenceAge
        d = dict(src_dict)
        boundary = check_platform_boundary_boundary(d.pop("boundary"))




        evidence = EvidenceAge.from_dict(d.pop("evidence"))




        state = check_platform_boundary_state(d.pop("state"))




        def _parse_image_digest(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        image_digest = _parse_image_digest(d.pop("image_digest", UNSET))


        def _parse_manifest_sha256(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        manifest_sha256 = _parse_manifest_sha256(d.pop("manifest_sha256", UNSET))


        def _parse_source_commit(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        source_commit = _parse_source_commit(d.pop("source_commit", UNSET))


        platform_boundary = cls(
            boundary=boundary,
            evidence=evidence,
            state=state,
            image_digest=image_digest,
            manifest_sha256=manifest_sha256,
            source_commit=source_commit,
        )

        return platform_boundary
