from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Literal, cast
from typing import Union

if TYPE_CHECKING:
  from ..models.runtime_image_receipt import RuntimeImageReceipt





T = TypeVar("T", bound="RunSwitchRuntimeImageResult")



@_attrs_define
class RunSwitchRuntimeImageResult:
    """
        Attributes:
            image_bytes (int):
            image_digest (str):
            oci_layout_sha256 (str):
            phase (Literal['prepare']):
            runtime_image (RuntimeImageReceipt): Strict schema-2 receipt persisted by the Controller image cache.

                ``oci_archive_sha256`` is the established filesystem/SQL receipt field.
                The compiled launch plan uses its own ``oci_layout_sha256`` field; the
                execution-plan service performs that one explicit typed projection at the
                plan boundary.
            subphase (Literal['runtime-image']):
            build_id (Union[None, Unset, str]):
            effective_execution_key (Union[None, Unset, str]):
     """

    image_bytes: int
    image_digest: str
    oci_layout_sha256: str
    phase: Literal['prepare']
    runtime_image: 'RuntimeImageReceipt'
    subphase: Literal['runtime-image']
    build_id: Union[None, Unset, str] = UNSET
    effective_execution_key: Union[None, Unset, str] = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.runtime_image_receipt import RuntimeImageReceipt
        image_bytes = self.image_bytes

        image_digest = self.image_digest

        oci_layout_sha256 = self.oci_layout_sha256

        phase = self.phase

        runtime_image = self.runtime_image.to_dict()

        subphase = self.subphase

        build_id: Union[None, Unset, str]
        if isinstance(self.build_id, Unset):
            build_id = UNSET
        else:
            build_id = self.build_id

        effective_execution_key: Union[None, Unset, str]
        if isinstance(self.effective_execution_key, Unset):
            effective_execution_key = UNSET
        else:
            effective_execution_key = self.effective_execution_key


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "image_bytes": image_bytes,
            "image_digest": image_digest,
            "oci_layout_sha256": oci_layout_sha256,
            "phase": phase,
            "runtime_image": runtime_image,
            "subphase": subphase,
        })
        if build_id is not UNSET:
            field_dict["build_id"] = build_id
        if effective_execution_key is not UNSET:
            field_dict["effective_execution_key"] = effective_execution_key

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.runtime_image_receipt import RuntimeImageReceipt
        d = dict(src_dict)
        image_bytes = d.pop("image_bytes")

        image_digest = d.pop("image_digest")

        oci_layout_sha256 = d.pop("oci_layout_sha256")

        phase = cast(Literal['prepare'] , d.pop("phase"))
        if phase != 'prepare':
            raise ValueError(f"phase must match const 'prepare', got '{phase}'")

        runtime_image = RuntimeImageReceipt.from_dict(d.pop("runtime_image"))




        subphase = cast(Literal['runtime-image'] , d.pop("subphase"))
        if subphase != 'runtime-image':
            raise ValueError(f"subphase must match const 'runtime-image', got '{subphase}'")

        def _parse_build_id(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        build_id = _parse_build_id(d.pop("build_id", UNSET))


        def _parse_effective_execution_key(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        effective_execution_key = _parse_effective_execution_key(d.pop("effective_execution_key", UNSET))


        run_switch_runtime_image_result = cls(
            image_bytes=image_bytes,
            image_digest=image_digest,
            oci_layout_sha256=oci_layout_sha256,
            phase=phase,
            runtime_image=runtime_image,
            subphase=subphase,
            build_id=build_id,
            effective_execution_key=effective_execution_key,
        )

        return run_switch_runtime_image_result
