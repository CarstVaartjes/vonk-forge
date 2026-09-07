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
  from ..models.controller_asset_state import ControllerAssetState
  from ..models.target_asset_state import TargetAssetState





T = TypeVar("T", bound="RuntimeImagePreparation")



@_attrs_define
class RuntimeImagePreparation:
    """ Exact executable OCI image kept separate from model payloads.

        Attributes:
            architecture (Literal['linux-arm64']):
            controller (ControllerAssetState): Availability of one immutable asset in Controller/NAS storage.
            image_bytes (int):
            image_digest (str):
            oci_layout_sha256 (str):
            runtime_interface (str):
            targets (list['TargetAssetState']):
            build_id (Union[None, Unset, str]):
     """

    architecture: Literal['linux-arm64']
    controller: 'ControllerAssetState'
    image_bytes: int
    image_digest: str
    oci_layout_sha256: str
    runtime_interface: str
    targets: list['TargetAssetState']
    build_id: Union[None, Unset, str] = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.controller_asset_state import ControllerAssetState
        from ..models.target_asset_state import TargetAssetState
        architecture = self.architecture

        controller = self.controller.to_dict()

        image_bytes = self.image_bytes

        image_digest = self.image_digest

        oci_layout_sha256 = self.oci_layout_sha256

        runtime_interface = self.runtime_interface

        targets = []
        for targets_item_data in self.targets:
            targets_item = targets_item_data.to_dict()
            targets.append(targets_item)



        build_id: Union[None, Unset, str]
        if isinstance(self.build_id, Unset):
            build_id = UNSET
        else:
            build_id = self.build_id


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "architecture": architecture,
            "controller": controller,
            "image_bytes": image_bytes,
            "image_digest": image_digest,
            "oci_layout_sha256": oci_layout_sha256,
            "runtime_interface": runtime_interface,
            "targets": targets,
        })
        if build_id is not UNSET:
            field_dict["build_id"] = build_id

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.controller_asset_state import ControllerAssetState
        from ..models.target_asset_state import TargetAssetState
        d = dict(src_dict)
        architecture = cast(Literal['linux-arm64'] , d.pop("architecture"))
        if architecture != 'linux-arm64':
            raise ValueError(f"architecture must match const 'linux-arm64', got '{architecture}'")

        controller = ControllerAssetState.from_dict(d.pop("controller"))




        image_bytes = d.pop("image_bytes")

        image_digest = d.pop("image_digest")

        oci_layout_sha256 = d.pop("oci_layout_sha256")

        runtime_interface = d.pop("runtime_interface")

        targets = []
        _targets = d.pop("targets")
        for targets_item_data in (_targets):
            targets_item = TargetAssetState.from_dict(targets_item_data)



            targets.append(targets_item)


        def _parse_build_id(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        build_id = _parse_build_id(d.pop("build_id", UNSET))


        runtime_image_preparation = cls(
            architecture=architecture,
            controller=controller,
            image_bytes=image_bytes,
            image_digest=image_digest,
            oci_layout_sha256=oci_layout_sha256,
            runtime_interface=runtime_interface,
            targets=targets,
            build_id=build_id,
        )

        return runtime_image_preparation
