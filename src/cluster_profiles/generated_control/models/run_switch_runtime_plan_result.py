from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast, Union
from typing import Literal, cast
from typing import Union






T = TypeVar("T", bound="RunSwitchRuntimePlanResult")



@_attrs_define
class RunSwitchRuntimePlanResult:
    """
        Attributes:
            compiled_plan_persisted (bool):
            install_plan_digest (str):
            installation_id (str):
            mapping_id (str):
            phase (Literal['prepare']):
            subphase (Literal['runtime-plan']):
            model_artifact_set_bytes (Union[None, Unset, int]):
            model_artifact_set_sha256 (Union[None, Unset, str]):
     """

    compiled_plan_persisted: bool
    install_plan_digest: str
    installation_id: str
    mapping_id: str
    phase: Literal['prepare']
    subphase: Literal['runtime-plan']
    model_artifact_set_bytes: Union[None, Unset, int] = UNSET
    model_artifact_set_sha256: Union[None, Unset, str] = UNSET





    def to_dict(self) -> dict[str, Any]:
        compiled_plan_persisted = self.compiled_plan_persisted

        install_plan_digest = self.install_plan_digest

        installation_id = self.installation_id

        mapping_id = self.mapping_id

        phase = self.phase

        subphase = self.subphase

        model_artifact_set_bytes: Union[None, Unset, int]
        if isinstance(self.model_artifact_set_bytes, Unset):
            model_artifact_set_bytes = UNSET
        else:
            model_artifact_set_bytes = self.model_artifact_set_bytes

        model_artifact_set_sha256: Union[None, Unset, str]
        if isinstance(self.model_artifact_set_sha256, Unset):
            model_artifact_set_sha256 = UNSET
        else:
            model_artifact_set_sha256 = self.model_artifact_set_sha256


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "compiled_plan_persisted": compiled_plan_persisted,
            "install_plan_digest": install_plan_digest,
            "installation_id": installation_id,
            "mapping_id": mapping_id,
            "phase": phase,
            "subphase": subphase,
        })
        if model_artifact_set_bytes is not UNSET:
            field_dict["model_artifact_set_bytes"] = model_artifact_set_bytes
        if model_artifact_set_sha256 is not UNSET:
            field_dict["model_artifact_set_sha256"] = model_artifact_set_sha256

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        compiled_plan_persisted = d.pop("compiled_plan_persisted")

        install_plan_digest = d.pop("install_plan_digest")

        installation_id = d.pop("installation_id")

        mapping_id = d.pop("mapping_id")

        phase = cast(Literal['prepare'] , d.pop("phase"))
        if phase != 'prepare':
            raise ValueError(f"phase must match const 'prepare', got '{phase}'")

        subphase = cast(Literal['runtime-plan'] , d.pop("subphase"))
        if subphase != 'runtime-plan':
            raise ValueError(f"subphase must match const 'runtime-plan', got '{subphase}'")

        def _parse_model_artifact_set_bytes(data: object) -> Union[None, Unset, int]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, int], data)

        model_artifact_set_bytes = _parse_model_artifact_set_bytes(d.pop("model_artifact_set_bytes", UNSET))


        def _parse_model_artifact_set_sha256(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        model_artifact_set_sha256 = _parse_model_artifact_set_sha256(d.pop("model_artifact_set_sha256", UNSET))


        run_switch_runtime_plan_result = cls(
            compiled_plan_persisted=compiled_plan_persisted,
            install_plan_digest=install_plan_digest,
            installation_id=installation_id,
            mapping_id=mapping_id,
            phase=phase,
            subphase=subphase,
            model_artifact_set_bytes=model_artifact_set_bytes,
            model_artifact_set_sha256=model_artifact_set_sha256,
        )

        return run_switch_runtime_plan_result
