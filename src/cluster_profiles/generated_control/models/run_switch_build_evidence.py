from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.run_switch_build_evidence_state import check_run_switch_build_evidence_state
from ..models.run_switch_build_evidence_state import RunSwitchBuildEvidenceState
from ..types import UNSET, Unset
from typing import cast

if TYPE_CHECKING:
  from ..models.build_compatibility_evidence import BuildCompatibilityEvidence
  from ..models.build_source_evidence import BuildSourceEvidence
  from ..models.runtime_image_storage_impact import RuntimeImageStorageImpact





T = TypeVar("T", bound="RunSwitchBuildEvidence")



@_attrs_define
class RunSwitchBuildEvidence:
    """
        Attributes:
            build_id (None | str):
            compatibility (BuildCompatibilityEvidence):
            image_digest (None | str):
            runtime (RuntimeImageStorageImpact):
            source (BuildSourceEvidence):
            state (RunSwitchBuildEvidenceState):
            build_input_sha256 (None | str | Unset):
            builder_node_id (None | str | Unset):
            detail (None | str | Unset):
            image_bytes (int | None | Unset):
            oci_layout_sha256 (None | str | Unset):
     """

    build_id: None | str
    compatibility: BuildCompatibilityEvidence
    image_digest: None | str
    runtime: RuntimeImageStorageImpact
    source: BuildSourceEvidence
    state: RunSwitchBuildEvidenceState
    build_input_sha256: None | str | Unset = UNSET
    builder_node_id: None | str | Unset = UNSET
    detail: None | str | Unset = UNSET
    image_bytes: int | None | Unset = UNSET
    oci_layout_sha256: None | str | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.build_compatibility_evidence import BuildCompatibilityEvidence # noqa: PLC0415
        from ..models.build_source_evidence import BuildSourceEvidence # noqa: PLC0415
        from ..models.runtime_image_storage_impact import RuntimeImageStorageImpact # noqa: PLC0415
        build_id: None | str
        build_id = self.build_id

        compatibility = self.compatibility.to_dict()

        image_digest: None | str
        image_digest = self.image_digest

        runtime = self.runtime.to_dict()

        source = self.source.to_dict()

        state: str = self.state

        build_input_sha256: None | str | Unset
        if isinstance(self.build_input_sha256, Unset):
            build_input_sha256 = UNSET
        else:
            build_input_sha256 = self.build_input_sha256

        builder_node_id: None | str | Unset
        if isinstance(self.builder_node_id, Unset):
            builder_node_id = UNSET
        else:
            builder_node_id = self.builder_node_id

        detail: None | str | Unset
        if isinstance(self.detail, Unset):
            detail = UNSET
        else:
            detail = self.detail

        image_bytes: int | None | Unset
        if isinstance(self.image_bytes, Unset):
            image_bytes = UNSET
        else:
            image_bytes = self.image_bytes

        oci_layout_sha256: None | str | Unset
        if isinstance(self.oci_layout_sha256, Unset):
            oci_layout_sha256 = UNSET
        else:
            oci_layout_sha256 = self.oci_layout_sha256


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "build_id": build_id,
            "compatibility": compatibility,
            "image_digest": image_digest,
            "runtime": runtime,
            "source": source,
            "state": state,
        })
        if build_input_sha256 is not UNSET:
            field_dict["build_input_sha256"] = build_input_sha256
        if builder_node_id is not UNSET:
            field_dict["builder_node_id"] = builder_node_id
        if detail is not UNSET:
            field_dict["detail"] = detail
        if image_bytes is not UNSET:
            field_dict["image_bytes"] = image_bytes
        if oci_layout_sha256 is not UNSET:
            field_dict["oci_layout_sha256"] = oci_layout_sha256

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.build_compatibility_evidence import BuildCompatibilityEvidence # noqa: PLC0415
        from ..models.build_source_evidence import BuildSourceEvidence # noqa: PLC0415
        from ..models.runtime_image_storage_impact import RuntimeImageStorageImpact # noqa: PLC0415
        d = dict(src_dict)
        def _parse_build_id(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        build_id = _parse_build_id(d.pop("build_id"))


        compatibility = BuildCompatibilityEvidence.from_dict(d.pop("compatibility"))




        def _parse_image_digest(data: object) -> None | str:
            if data is None:
                return data
            return cast(None | str, data)

        image_digest = _parse_image_digest(d.pop("image_digest"))


        runtime = RuntimeImageStorageImpact.from_dict(d.pop("runtime"))




        source = BuildSourceEvidence.from_dict(d.pop("source"))




        state = check_run_switch_build_evidence_state(d.pop("state"))




        def _parse_build_input_sha256(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        build_input_sha256 = _parse_build_input_sha256(d.pop("build_input_sha256", UNSET))


        def _parse_builder_node_id(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        builder_node_id = _parse_builder_node_id(d.pop("builder_node_id", UNSET))


        def _parse_detail(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        detail = _parse_detail(d.pop("detail", UNSET))


        def _parse_image_bytes(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        image_bytes = _parse_image_bytes(d.pop("image_bytes", UNSET))


        def _parse_oci_layout_sha256(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        oci_layout_sha256 = _parse_oci_layout_sha256(d.pop("oci_layout_sha256", UNSET))


        run_switch_build_evidence = cls(
            build_id=build_id,
            compatibility=compatibility,
            image_digest=image_digest,
            runtime=runtime,
            source=source,
            state=state,
            build_input_sha256=build_input_sha256,
            builder_node_id=builder_node_id,
            detail=detail,
            image_bytes=image_bytes,
            oci_layout_sha256=oci_layout_sha256,
        )

        return run_switch_build_evidence
