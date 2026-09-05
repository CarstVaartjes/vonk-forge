from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.recipe_build_evidence_state import check_recipe_build_evidence_state
from ..models.recipe_build_evidence_state import RecipeBuildEvidenceState
from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Union

if TYPE_CHECKING:
  from ..models.build_compatibility_evidence import BuildCompatibilityEvidence
  from ..models.runtime_image_storage_impact import RuntimeImageStorageImpact
  from ..models.build_source_evidence import BuildSourceEvidence





T = TypeVar("T", bound="RecipeBuildEvidence")



@_attrs_define
class RecipeBuildEvidence:
    """
        Attributes:
            build_id (Union[None, str]):
            compatibility (BuildCompatibilityEvidence):
            image_digest (Union[None, str]):
            runtime (RuntimeImageStorageImpact):
            source (BuildSourceEvidence):
            state (RecipeBuildEvidenceState):
            build_input_sha256 (Union[None, Unset, str]):
            builder_node_id (Union[None, Unset, str]):
            detail (Union[None, Unset, str]):
            image_bytes (Union[None, Unset, int]):
            oci_layout_sha256 (Union[None, Unset, str]):
     """

    build_id: Union[None, str]
    compatibility: 'BuildCompatibilityEvidence'
    image_digest: Union[None, str]
    runtime: 'RuntimeImageStorageImpact'
    source: 'BuildSourceEvidence'
    state: RecipeBuildEvidenceState
    build_input_sha256: Union[None, Unset, str] = UNSET
    builder_node_id: Union[None, Unset, str] = UNSET
    detail: Union[None, Unset, str] = UNSET
    image_bytes: Union[None, Unset, int] = UNSET
    oci_layout_sha256: Union[None, Unset, str] = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.build_compatibility_evidence import BuildCompatibilityEvidence
        from ..models.runtime_image_storage_impact import RuntimeImageStorageImpact
        from ..models.build_source_evidence import BuildSourceEvidence
        build_id: Union[None, str]
        build_id = self.build_id

        compatibility = self.compatibility.to_dict()

        image_digest: Union[None, str]
        image_digest = self.image_digest

        runtime = self.runtime.to_dict()

        source = self.source.to_dict()

        state: str = self.state

        build_input_sha256: Union[None, Unset, str]
        if isinstance(self.build_input_sha256, Unset):
            build_input_sha256 = UNSET
        else:
            build_input_sha256 = self.build_input_sha256

        builder_node_id: Union[None, Unset, str]
        if isinstance(self.builder_node_id, Unset):
            builder_node_id = UNSET
        else:
            builder_node_id = self.builder_node_id

        detail: Union[None, Unset, str]
        if isinstance(self.detail, Unset):
            detail = UNSET
        else:
            detail = self.detail

        image_bytes: Union[None, Unset, int]
        if isinstance(self.image_bytes, Unset):
            image_bytes = UNSET
        else:
            image_bytes = self.image_bytes

        oci_layout_sha256: Union[None, Unset, str]
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
        from ..models.build_compatibility_evidence import BuildCompatibilityEvidence
        from ..models.runtime_image_storage_impact import RuntimeImageStorageImpact
        from ..models.build_source_evidence import BuildSourceEvidence
        d = dict(src_dict)
        def _parse_build_id(data: object) -> Union[None, str]:
            if data is None:
                return data
            return cast(Union[None, str], data)

        build_id = _parse_build_id(d.pop("build_id"))


        compatibility = BuildCompatibilityEvidence.from_dict(d.pop("compatibility"))




        def _parse_image_digest(data: object) -> Union[None, str]:
            if data is None:
                return data
            return cast(Union[None, str], data)

        image_digest = _parse_image_digest(d.pop("image_digest"))


        runtime = RuntimeImageStorageImpact.from_dict(d.pop("runtime"))




        source = BuildSourceEvidence.from_dict(d.pop("source"))




        state = check_recipe_build_evidence_state(d.pop("state"))




        def _parse_build_input_sha256(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        build_input_sha256 = _parse_build_input_sha256(d.pop("build_input_sha256", UNSET))


        def _parse_builder_node_id(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        builder_node_id = _parse_builder_node_id(d.pop("builder_node_id", UNSET))


        def _parse_detail(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        detail = _parse_detail(d.pop("detail", UNSET))


        def _parse_image_bytes(data: object) -> Union[None, Unset, int]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, int], data)

        image_bytes = _parse_image_bytes(d.pop("image_bytes", UNSET))


        def _parse_oci_layout_sha256(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        oci_layout_sha256 = _parse_oci_layout_sha256(d.pop("oci_layout_sha256", UNSET))


        recipe_build_evidence = cls(
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

        return recipe_build_evidence
