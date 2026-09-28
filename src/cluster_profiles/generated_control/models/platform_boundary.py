from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.platform_boundary_boundary import check_platform_boundary_boundary
from ..models.platform_boundary_boundary import PlatformBoundaryBoundary
from ..models.platform_boundary_state import check_platform_boundary_state
from ..models.platform_boundary_state import PlatformBoundaryState
from ..types import UNSET, Unset
from typing import cast

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
            image_digest (None | str | Unset):
            manifest_sha256 (None | str | Unset):
            source_commit (None | str | Unset):
     """

    boundary: PlatformBoundaryBoundary
    evidence: EvidenceAge
    state: PlatformBoundaryState
    image_digest: None | str | Unset = UNSET
    manifest_sha256: None | str | Unset = UNSET
    source_commit: None | str | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.evidence_age import EvidenceAge # noqa: PLC0415
        boundary: str = self.boundary

        evidence = self.evidence.to_dict()

        state: str = self.state

        image_digest: None | str | Unset
        if isinstance(self.image_digest, Unset):
            image_digest = UNSET
        else:
            image_digest = self.image_digest

        manifest_sha256: None | str | Unset
        if isinstance(self.manifest_sha256, Unset):
            manifest_sha256 = UNSET
        else:
            manifest_sha256 = self.manifest_sha256

        source_commit: None | str | Unset
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
        from ..models.evidence_age import EvidenceAge # noqa: PLC0415
        d = dict(src_dict)
        boundary = check_platform_boundary_boundary(d.pop("boundary"))




        evidence = EvidenceAge.from_dict(d.pop("evidence"))




        state = check_platform_boundary_state(d.pop("state"))




        def _parse_image_digest(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        image_digest = _parse_image_digest(d.pop("image_digest", UNSET))


        def _parse_manifest_sha256(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        manifest_sha256 = _parse_manifest_sha256(d.pop("manifest_sha256", UNSET))


        def _parse_source_commit(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

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
