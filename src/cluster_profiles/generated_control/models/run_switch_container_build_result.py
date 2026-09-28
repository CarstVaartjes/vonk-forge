from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.run_switch_container_build_result_state import check_run_switch_container_build_result_state
from ..models.run_switch_container_build_result_state import RunSwitchContainerBuildResultState
from ..types import UNSET, Unset
from typing import cast
from typing import Literal, cast






T = TypeVar("T", bound="RunSwitchContainerBuildResult")



@_attrs_define
class RunSwitchContainerBuildResult:
    """
        Attributes:
            build_id (str):
            build_input_sha256 (str):
            phase (Literal['prepare']):
            state (RunSwitchContainerBuildResultState):
            subphase (Literal['container-build']):
            image_bytes (int | None | Unset):
            image_digest (None | str | Unset):
            oci_layout_sha256 (None | str | Unset):
     """

    build_id: str
    build_input_sha256: str
    phase: Literal['prepare']
    state: RunSwitchContainerBuildResultState
    subphase: Literal['container-build']
    image_bytes: int | None | Unset = UNSET
    image_digest: None | str | Unset = UNSET
    oci_layout_sha256: None | str | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        build_id = self.build_id

        build_input_sha256 = self.build_input_sha256

        phase = self.phase

        state: str = self.state

        subphase = self.subphase

        image_bytes: int | None | Unset
        if isinstance(self.image_bytes, Unset):
            image_bytes = UNSET
        else:
            image_bytes = self.image_bytes

        image_digest: None | str | Unset
        if isinstance(self.image_digest, Unset):
            image_digest = UNSET
        else:
            image_digest = self.image_digest

        oci_layout_sha256: None | str | Unset
        if isinstance(self.oci_layout_sha256, Unset):
            oci_layout_sha256 = UNSET
        else:
            oci_layout_sha256 = self.oci_layout_sha256


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "build_id": build_id,
            "build_input_sha256": build_input_sha256,
            "phase": phase,
            "state": state,
            "subphase": subphase,
        })
        if image_bytes is not UNSET:
            field_dict["image_bytes"] = image_bytes
        if image_digest is not UNSET:
            field_dict["image_digest"] = image_digest
        if oci_layout_sha256 is not UNSET:
            field_dict["oci_layout_sha256"] = oci_layout_sha256

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        build_id = d.pop("build_id")

        build_input_sha256 = d.pop("build_input_sha256")

        phase = cast(Literal['prepare'] , d.pop("phase"))
        if phase != 'prepare':
            raise ValueError(f"phase must match const 'prepare', got '{phase}'")

        state = check_run_switch_container_build_result_state(d.pop("state"))




        subphase = cast(Literal['container-build'] , d.pop("subphase"))
        if subphase != 'container-build':
            raise ValueError(f"subphase must match const 'container-build', got '{subphase}'")

        def _parse_image_bytes(data: object) -> int | None | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(int | None | Unset, data)

        image_bytes = _parse_image_bytes(d.pop("image_bytes", UNSET))


        def _parse_image_digest(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        image_digest = _parse_image_digest(d.pop("image_digest", UNSET))


        def _parse_oci_layout_sha256(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        oci_layout_sha256 = _parse_oci_layout_sha256(d.pop("oci_layout_sha256", UNSET))


        run_switch_container_build_result = cls(
            build_id=build_id,
            build_input_sha256=build_input_sha256,
            phase=phase,
            state=state,
            subphase=subphase,
            image_bytes=image_bytes,
            image_digest=image_digest,
            oci_layout_sha256=oci_layout_sha256,
        )

        return run_switch_container_build_result
