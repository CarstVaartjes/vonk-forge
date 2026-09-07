from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.compiled_artifact_contract_interface import check_compiled_artifact_contract_interface
from ..models.compiled_artifact_contract_interface import CompiledArtifactContractInterface
from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Literal, cast
from typing import Union

if TYPE_CHECKING:
  from ..models.integer_parameter import IntegerParameter
  from ..models.enum_parameter import EnumParameter
  from ..models.string_parameter import StringParameter
  from ..models.boolean_parameter import BooleanParameter
  from ..models.float_parameter import FloatParameter
  from ..models.artifact_output_contract import ArtifactOutputContract
  from ..models.artifact_input_contract import ArtifactInputContract
  from ..models.artifact_output_limits import ArtifactOutputLimits
  from ..models.compiled_artifact_contract_engine_type_0 import CompiledArtifactContractEngineType0





T = TypeVar("T", bound="CompiledArtifactContract")



@_attrs_define
class CompiledArtifactContract:
    """ The canonical typed artifact execution contract.

        Attributes:
            input_ (ArtifactInputContract):
            interface (CompiledArtifactContractInterface):
            max_timeout_seconds (int):
            output (ArtifactOutputContract):
            output_limits (ArtifactOutputLimits):
            parameters (list[Union['BooleanParameter', 'EnumParameter', 'FloatParameter', 'IntegerParameter',
                'StringParameter']]):
            schema_version (Literal[1]):
            engine (Union['CompiledArtifactContractEngineType0', None, Unset]):
     """

    input_: 'ArtifactInputContract'
    interface: CompiledArtifactContractInterface
    max_timeout_seconds: int
    output: 'ArtifactOutputContract'
    output_limits: 'ArtifactOutputLimits'
    parameters: list[Union['BooleanParameter', 'EnumParameter', 'FloatParameter', 'IntegerParameter', 'StringParameter']]
    schema_version: Literal[1]
    engine: Union['CompiledArtifactContractEngineType0', None, Unset] = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.integer_parameter import IntegerParameter
        from ..models.enum_parameter import EnumParameter
        from ..models.string_parameter import StringParameter
        from ..models.boolean_parameter import BooleanParameter
        from ..models.float_parameter import FloatParameter
        from ..models.artifact_output_contract import ArtifactOutputContract
        from ..models.artifact_input_contract import ArtifactInputContract
        from ..models.artifact_output_limits import ArtifactOutputLimits
        from ..models.compiled_artifact_contract_engine_type_0 import CompiledArtifactContractEngineType0
        input_ = self.input_.to_dict()

        interface: str = self.interface

        max_timeout_seconds = self.max_timeout_seconds

        output = self.output.to_dict()

        output_limits = self.output_limits.to_dict()

        parameters = []
        for parameters_item_data in self.parameters:
            parameters_item: dict[str, Any]
            if isinstance(parameters_item_data, StringParameter):
                parameters_item = parameters_item_data.to_dict()
            elif isinstance(parameters_item_data, IntegerParameter):
                parameters_item = parameters_item_data.to_dict()
            elif isinstance(parameters_item_data, FloatParameter):
                parameters_item = parameters_item_data.to_dict()
            elif isinstance(parameters_item_data, BooleanParameter):
                parameters_item = parameters_item_data.to_dict()
            else:
                parameters_item = parameters_item_data.to_dict()

            parameters.append(parameters_item)



        schema_version = self.schema_version

        engine: Union[None, Unset, dict[str, Any]]
        if isinstance(self.engine, Unset):
            engine = UNSET
        elif isinstance(self.engine, CompiledArtifactContractEngineType0):
            engine = self.engine.to_dict()
        else:
            engine = self.engine


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "input": input_,
            "interface": interface,
            "max_timeout_seconds": max_timeout_seconds,
            "output": output,
            "output_limits": output_limits,
            "parameters": parameters,
            "schema_version": schema_version,
        })
        if engine is not UNSET:
            field_dict["engine"] = engine

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.integer_parameter import IntegerParameter
        from ..models.enum_parameter import EnumParameter
        from ..models.string_parameter import StringParameter
        from ..models.boolean_parameter import BooleanParameter
        from ..models.float_parameter import FloatParameter
        from ..models.artifact_output_contract import ArtifactOutputContract
        from ..models.artifact_input_contract import ArtifactInputContract
        from ..models.artifact_output_limits import ArtifactOutputLimits
        from ..models.compiled_artifact_contract_engine_type_0 import CompiledArtifactContractEngineType0
        d = dict(src_dict)
        input_ = ArtifactInputContract.from_dict(d.pop("input"))




        interface = check_compiled_artifact_contract_interface(d.pop("interface"))




        max_timeout_seconds = d.pop("max_timeout_seconds")

        output = ArtifactOutputContract.from_dict(d.pop("output"))




        output_limits = ArtifactOutputLimits.from_dict(d.pop("output_limits"))




        parameters = []
        _parameters = d.pop("parameters")
        for parameters_item_data in (_parameters):
            def _parse_parameters_item(data: object) -> Union['BooleanParameter', 'EnumParameter', 'FloatParameter', 'IntegerParameter', 'StringParameter']:
                try:
                    if not isinstance(data, dict):
                        raise TypeError()
                    componentsschemas_parameter_definition_type_0 = StringParameter.from_dict(data)



                    return componentsschemas_parameter_definition_type_0
                except: # noqa: E722
                    pass
                try:
                    if not isinstance(data, dict):
                        raise TypeError()
                    componentsschemas_parameter_definition_type_1 = IntegerParameter.from_dict(data)



                    return componentsschemas_parameter_definition_type_1
                except: # noqa: E722
                    pass
                try:
                    if not isinstance(data, dict):
                        raise TypeError()
                    componentsschemas_parameter_definition_type_2 = FloatParameter.from_dict(data)



                    return componentsschemas_parameter_definition_type_2
                except: # noqa: E722
                    pass
                try:
                    if not isinstance(data, dict):
                        raise TypeError()
                    componentsschemas_parameter_definition_type_3 = BooleanParameter.from_dict(data)



                    return componentsschemas_parameter_definition_type_3
                except: # noqa: E722
                    pass
                if not isinstance(data, dict):
                    raise TypeError()
                componentsschemas_parameter_definition_type_4 = EnumParameter.from_dict(data)



                return componentsschemas_parameter_definition_type_4

            parameters_item = _parse_parameters_item(parameters_item_data)

            parameters.append(parameters_item)


        schema_version = cast(Literal[1] , d.pop("schema_version"))
        if schema_version != 1:
            raise ValueError(f"schema_version must match const 1, got '{schema_version}'")

        def _parse_engine(data: object) -> Union['CompiledArtifactContractEngineType0', None, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                engine_type_0 = CompiledArtifactContractEngineType0.from_dict(data)



                return engine_type_0
            except: # noqa: E722
                pass
            return cast(Union['CompiledArtifactContractEngineType0', None, Unset], data)

        engine = _parse_engine(d.pop("engine", UNSET))


        compiled_artifact_contract = cls(
            input_=input_,
            interface=interface,
            max_timeout_seconds=max_timeout_seconds,
            output=output,
            output_limits=output_limits,
            parameters=parameters,
            schema_version=schema_version,
            engine=engine,
        )

        return compiled_artifact_contract
