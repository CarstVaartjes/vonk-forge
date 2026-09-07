from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.artifact_job_response_interface import ArtifactJobResponseInterface
from ..models.artifact_job_response_interface import check_artifact_job_response_interface
from ..models.artifact_job_response_state import ArtifactJobResponseState
from ..models.artifact_job_response_state import check_artifact_job_response_state
from ..types import UNSET, Unset
from dateutil.parser import isoparse
from typing import cast
from typing import cast, Union
from typing import Union
import datetime

if TYPE_CHECKING:
  from ..models.artifact_file_declaration import ArtifactFileDeclaration
  from ..models.output_limits import OutputLimits
  from ..models.artifact_job_result_evidence import ArtifactJobResultEvidence
  from ..models.artifact_output_file import ArtifactOutputFile
  from ..models.artifact_job_response_compiled_contract import ArtifactJobResponseCompiledContract





T = TypeVar("T", bound="ArtifactJobResponse")



@_attrs_define
class ArtifactJobResponse:
    """
        Attributes:
            compiled_contract (ArtifactJobResponseCompiledContract):
            contract_sha256 (str):
            created_at (datetime.datetime):
            id (str):
            input_declarations (list['ArtifactFileDeclaration']):
            input_files (list['ArtifactFileDeclaration']):
            input_manifest_sha256 (str):
            input_total_bytes (int):
            interface (ArtifactJobResponseInterface):
            output_files (list['ArtifactOutputFile']):
            output_limits (OutputLimits):
            run_id (str):
            state (ArtifactJobResponseState):
            timeout_seconds (int):
            updated_at (datetime.datetime):
            operation_id (Union[None, Unset, str]):
            output_manifest_sha256 (Union[None, Unset, str]):
            result_evidence (Union['ArtifactJobResultEvidence', None, Unset]):
            status_reason (Union[None, Unset, str]):
     """

    compiled_contract: 'ArtifactJobResponseCompiledContract'
    contract_sha256: str
    created_at: datetime.datetime
    id: str
    input_declarations: list['ArtifactFileDeclaration']
    input_files: list['ArtifactFileDeclaration']
    input_manifest_sha256: str
    input_total_bytes: int
    interface: ArtifactJobResponseInterface
    output_files: list['ArtifactOutputFile']
    output_limits: 'OutputLimits'
    run_id: str
    state: ArtifactJobResponseState
    timeout_seconds: int
    updated_at: datetime.datetime
    operation_id: Union[None, Unset, str] = UNSET
    output_manifest_sha256: Union[None, Unset, str] = UNSET
    result_evidence: Union['ArtifactJobResultEvidence', None, Unset] = UNSET
    status_reason: Union[None, Unset, str] = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.artifact_file_declaration import ArtifactFileDeclaration
        from ..models.output_limits import OutputLimits
        from ..models.artifact_job_result_evidence import ArtifactJobResultEvidence
        from ..models.artifact_output_file import ArtifactOutputFile
        from ..models.artifact_job_response_compiled_contract import ArtifactJobResponseCompiledContract
        compiled_contract = self.compiled_contract.to_dict()

        contract_sha256 = self.contract_sha256

        created_at = self.created_at.isoformat()

        id = self.id

        input_declarations = []
        for input_declarations_item_data in self.input_declarations:
            input_declarations_item = input_declarations_item_data.to_dict()
            input_declarations.append(input_declarations_item)



        input_files = []
        for input_files_item_data in self.input_files:
            input_files_item = input_files_item_data.to_dict()
            input_files.append(input_files_item)



        input_manifest_sha256 = self.input_manifest_sha256

        input_total_bytes = self.input_total_bytes

        interface: str = self.interface

        output_files = []
        for output_files_item_data in self.output_files:
            output_files_item = output_files_item_data.to_dict()
            output_files.append(output_files_item)



        output_limits = self.output_limits.to_dict()

        run_id = self.run_id

        state: str = self.state

        timeout_seconds = self.timeout_seconds

        updated_at = self.updated_at.isoformat()

        operation_id: Union[None, Unset, str]
        if isinstance(self.operation_id, Unset):
            operation_id = UNSET
        else:
            operation_id = self.operation_id

        output_manifest_sha256: Union[None, Unset, str]
        if isinstance(self.output_manifest_sha256, Unset):
            output_manifest_sha256 = UNSET
        else:
            output_manifest_sha256 = self.output_manifest_sha256

        result_evidence: Union[None, Unset, dict[str, Any]]
        if isinstance(self.result_evidence, Unset):
            result_evidence = UNSET
        elif isinstance(self.result_evidence, ArtifactJobResultEvidence):
            result_evidence = self.result_evidence.to_dict()
        else:
            result_evidence = self.result_evidence

        status_reason: Union[None, Unset, str]
        if isinstance(self.status_reason, Unset):
            status_reason = UNSET
        else:
            status_reason = self.status_reason


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "compiled_contract": compiled_contract,
            "contract_sha256": contract_sha256,
            "created_at": created_at,
            "id": id,
            "input_declarations": input_declarations,
            "input_files": input_files,
            "input_manifest_sha256": input_manifest_sha256,
            "input_total_bytes": input_total_bytes,
            "interface": interface,
            "output_files": output_files,
            "output_limits": output_limits,
            "run_id": run_id,
            "state": state,
            "timeout_seconds": timeout_seconds,
            "updated_at": updated_at,
        })
        if operation_id is not UNSET:
            field_dict["operation_id"] = operation_id
        if output_manifest_sha256 is not UNSET:
            field_dict["output_manifest_sha256"] = output_manifest_sha256
        if result_evidence is not UNSET:
            field_dict["result_evidence"] = result_evidence
        if status_reason is not UNSET:
            field_dict["status_reason"] = status_reason

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.artifact_file_declaration import ArtifactFileDeclaration
        from ..models.output_limits import OutputLimits
        from ..models.artifact_job_result_evidence import ArtifactJobResultEvidence
        from ..models.artifact_output_file import ArtifactOutputFile
        from ..models.artifact_job_response_compiled_contract import ArtifactJobResponseCompiledContract
        d = dict(src_dict)
        compiled_contract = ArtifactJobResponseCompiledContract.from_dict(d.pop("compiled_contract"))




        contract_sha256 = d.pop("contract_sha256")

        created_at = isoparse(d.pop("created_at"))




        id = d.pop("id")

        input_declarations = []
        _input_declarations = d.pop("input_declarations")
        for input_declarations_item_data in (_input_declarations):
            input_declarations_item = ArtifactFileDeclaration.from_dict(input_declarations_item_data)



            input_declarations.append(input_declarations_item)


        input_files = []
        _input_files = d.pop("input_files")
        for input_files_item_data in (_input_files):
            input_files_item = ArtifactFileDeclaration.from_dict(input_files_item_data)



            input_files.append(input_files_item)


        input_manifest_sha256 = d.pop("input_manifest_sha256")

        input_total_bytes = d.pop("input_total_bytes")

        interface = check_artifact_job_response_interface(d.pop("interface"))




        output_files = []
        _output_files = d.pop("output_files")
        for output_files_item_data in (_output_files):
            output_files_item = ArtifactOutputFile.from_dict(output_files_item_data)



            output_files.append(output_files_item)


        output_limits = OutputLimits.from_dict(d.pop("output_limits"))




        run_id = d.pop("run_id")

        state = check_artifact_job_response_state(d.pop("state"))




        timeout_seconds = d.pop("timeout_seconds")

        updated_at = isoparse(d.pop("updated_at"))




        def _parse_operation_id(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        operation_id = _parse_operation_id(d.pop("operation_id", UNSET))


        def _parse_output_manifest_sha256(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        output_manifest_sha256 = _parse_output_manifest_sha256(d.pop("output_manifest_sha256", UNSET))


        def _parse_result_evidence(data: object) -> Union['ArtifactJobResultEvidence', None, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                result_evidence_type_0 = ArtifactJobResultEvidence.from_dict(data)



                return result_evidence_type_0
            except: # noqa: E722
                pass
            return cast(Union['ArtifactJobResultEvidence', None, Unset], data)

        result_evidence = _parse_result_evidence(d.pop("result_evidence", UNSET))


        def _parse_status_reason(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        status_reason = _parse_status_reason(d.pop("status_reason", UNSET))


        artifact_job_response = cls(
            compiled_contract=compiled_contract,
            contract_sha256=contract_sha256,
            created_at=created_at,
            id=id,
            input_declarations=input_declarations,
            input_files=input_files,
            input_manifest_sha256=input_manifest_sha256,
            input_total_bytes=input_total_bytes,
            interface=interface,
            output_files=output_files,
            output_limits=output_limits,
            run_id=run_id,
            state=state,
            timeout_seconds=timeout_seconds,
            updated_at=updated_at,
            operation_id=operation_id,
            output_manifest_sha256=output_manifest_sha256,
            result_evidence=result_evidence,
            status_reason=status_reason,
        )

        return artifact_job_response
