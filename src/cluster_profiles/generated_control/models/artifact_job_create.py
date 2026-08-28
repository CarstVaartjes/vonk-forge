from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
from typing import Union

if TYPE_CHECKING:
  from ..models.output_limits import OutputLimits
  from ..models.artifact_file_declaration import ArtifactFileDeclaration
  from ..models.artifact_job_create_parameters import ArtifactJobCreateParameters





T = TypeVar("T", bound="ArtifactJobCreate")



@_attrs_define
class ArtifactJobCreate:
    """
        Attributes:
            interface (str):
            output_limits (OutputLimits):
            timeout_seconds (int):
            inputs (Union[Unset, list['ArtifactFileDeclaration']]):
            parameters (Union[Unset, ArtifactJobCreateParameters]):
     """

    interface: str
    output_limits: 'OutputLimits'
    timeout_seconds: int
    inputs: Union[Unset, list['ArtifactFileDeclaration']] = UNSET
    parameters: Union[Unset, 'ArtifactJobCreateParameters'] = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.output_limits import OutputLimits
        from ..models.artifact_file_declaration import ArtifactFileDeclaration
        from ..models.artifact_job_create_parameters import ArtifactJobCreateParameters
        interface = self.interface

        output_limits = self.output_limits.to_dict()

        timeout_seconds = self.timeout_seconds

        inputs: Union[Unset, list[dict[str, Any]]] = UNSET
        if not isinstance(self.inputs, Unset):
            inputs = []
            for inputs_item_data in self.inputs:
                inputs_item = inputs_item_data.to_dict()
                inputs.append(inputs_item)



        parameters: Union[Unset, dict[str, Any]] = UNSET
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
        from ..models.output_limits import OutputLimits
        from ..models.artifact_file_declaration import ArtifactFileDeclaration
        from ..models.artifact_job_create_parameters import ArtifactJobCreateParameters
        d = dict(src_dict)
        interface = d.pop("interface")

        output_limits = OutputLimits.from_dict(d.pop("output_limits"))




        timeout_seconds = d.pop("timeout_seconds")

        inputs = []
        _inputs = d.pop("inputs", UNSET)
        for inputs_item_data in (_inputs or []):
            inputs_item = ArtifactFileDeclaration.from_dict(inputs_item_data)



            inputs.append(inputs_item)


        _parameters = d.pop("parameters", UNSET)
        parameters: Union[Unset, ArtifactJobCreateParameters]
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
