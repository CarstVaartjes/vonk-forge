from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast

if TYPE_CHECKING:
  from ..models.artifact_file_declaration import ArtifactFileDeclaration
  from ..models.artifact_job_create_parameters import ArtifactJobCreateParameters
  from ..models.output_limits import OutputLimits





T = TypeVar("T", bound="ArtifactJobCreate")



@_attrs_define
class ArtifactJobCreate:
    """
        Attributes:
            interface (str):
            output_limits (OutputLimits):
            timeout_seconds (int):
            inputs (list[ArtifactFileDeclaration] | Unset):
            parameters (ArtifactJobCreateParameters | Unset):
     """

    interface: str
    output_limits: OutputLimits
    timeout_seconds: int
    inputs: list[ArtifactFileDeclaration] | Unset = UNSET
    parameters: ArtifactJobCreateParameters | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.artifact_file_declaration import ArtifactFileDeclaration # noqa: PLC0415
        from ..models.artifact_job_create_parameters import ArtifactJobCreateParameters # noqa: PLC0415
        from ..models.output_limits import OutputLimits # noqa: PLC0415
        interface = self.interface

        output_limits = self.output_limits.to_dict()

        timeout_seconds = self.timeout_seconds

        inputs: list[dict[str, Any]] | Unset = UNSET
        if not isinstance(self.inputs, Unset):
            inputs = []
            for inputs_item_data in self.inputs:
                inputs_item = inputs_item_data.to_dict()
                inputs.append(inputs_item)



        parameters: dict[str, Any] | Unset = UNSET
        if not isinstance(self.parameters, Unset):
            parameters = self.parameters.to_dict()


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "interface": interface,
            "output_limits": output_limits,
            "timeout_seconds": timeout_seconds,
        })
        if inputs is not UNSET:
            field_dict["inputs"] = inputs
        if parameters is not UNSET:
            field_dict["parameters"] = parameters

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.artifact_file_declaration import ArtifactFileDeclaration # noqa: PLC0415
        from ..models.artifact_job_create_parameters import ArtifactJobCreateParameters # noqa: PLC0415
        from ..models.output_limits import OutputLimits # noqa: PLC0415
        d = dict(src_dict)
        interface = d.pop("interface")

        output_limits = OutputLimits.from_dict(d.pop("output_limits"))




        timeout_seconds = d.pop("timeout_seconds")

        _inputs = d.pop("inputs", UNSET)
        inputs: list[ArtifactFileDeclaration] | Unset = UNSET
        if _inputs is not UNSET:
            inputs = []
            for inputs_item_data in _inputs:
                inputs_item = ArtifactFileDeclaration.from_dict(inputs_item_data)



                inputs.append(inputs_item)


        _parameters = d.pop("parameters", UNSET)
        parameters: ArtifactJobCreateParameters | Unset
        if isinstance(_parameters,  Unset):
            parameters = UNSET
        else:
            parameters = ArtifactJobCreateParameters.from_dict(_parameters)




        artifact_job_create = cls(
            interface=interface,
            output_limits=output_limits,
            timeout_seconds=timeout_seconds,
            inputs=inputs,
            parameters=parameters,
        )

        return artifact_job_create
