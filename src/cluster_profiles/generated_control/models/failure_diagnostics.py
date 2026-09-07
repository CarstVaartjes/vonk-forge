from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.failure_diagnostics_category import check_failure_diagnostics_category
from ..models.failure_diagnostics_category import FailureDiagnosticsCategory
from ..types import UNSET, Unset
from typing import cast
from typing import Literal, Union, cast

if TYPE_CHECKING:
  from ..models.failure_log_tail import FailureLogTail
  from ..models.failure_property import FailureProperty





T = TypeVar("T", bound="FailureDiagnostics")



@_attrs_define
class FailureDiagnostics:
    """
        Attributes:
            category (FailureDiagnosticsCategory):
            collected_at (str):
            collector_errors (list[str]):
            phase (str):
            preflight (list['FailureProperty']):
            sandbox (list['FailureProperty']):
            stderr (FailureLogTail):
            stdout (FailureLogTail):
            storage (list['FailureProperty']):
            versions (list['FailureProperty']):
            schema_version (Union[Literal[1], Unset]):  Default: 1.
     """

    category: FailureDiagnosticsCategory
    collected_at: str
    collector_errors: list[str]
    phase: str
    preflight: list['FailureProperty']
    sandbox: list['FailureProperty']
    stderr: 'FailureLogTail'
    stdout: 'FailureLogTail'
    storage: list['FailureProperty']
    versions: list['FailureProperty']
    schema_version: Union[Literal[1], Unset] = 1





    def to_dict(self) -> dict[str, Any]:
        from ..models.failure_log_tail import FailureLogTail
        from ..models.failure_property import FailureProperty
        category: str = self.category

        collected_at = self.collected_at

        collector_errors = self.collector_errors



        phase = self.phase

        preflight = []
        for preflight_item_data in self.preflight:
            preflight_item = preflight_item_data.to_dict()
            preflight.append(preflight_item)



        sandbox = []
        for sandbox_item_data in self.sandbox:
            sandbox_item = sandbox_item_data.to_dict()
            sandbox.append(sandbox_item)



        stderr = self.stderr.to_dict()

        stdout = self.stdout.to_dict()

        storage = []
        for storage_item_data in self.storage:
            storage_item = storage_item_data.to_dict()
            storage.append(storage_item)



        versions = []
        for versions_item_data in self.versions:
            versions_item = versions_item_data.to_dict()
            versions.append(versions_item)



        schema_version = self.schema_version


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "category": category,
            "collected_at": collected_at,
            "collector_errors": collector_errors,
            "phase": phase,
            "preflight": preflight,
            "sandbox": sandbox,
            "stderr": stderr,
            "stdout": stdout,
            "storage": storage,
            "versions": versions,
        })
        if schema_version is not UNSET:
            field_dict["schema_version"] = schema_version

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.failure_log_tail import FailureLogTail
        from ..models.failure_property import FailureProperty
        d = dict(src_dict)
        category = check_failure_diagnostics_category(d.pop("category"))




        collected_at = d.pop("collected_at")

        collector_errors = cast(list[str], d.pop("collector_errors"))


        phase = d.pop("phase")

        preflight = []
        _preflight = d.pop("preflight")
        for preflight_item_data in (_preflight):
            preflight_item = FailureProperty.from_dict(preflight_item_data)



            preflight.append(preflight_item)


        sandbox = []
        _sandbox = d.pop("sandbox")
        for sandbox_item_data in (_sandbox):
            sandbox_item = FailureProperty.from_dict(sandbox_item_data)



            sandbox.append(sandbox_item)


        stderr = FailureLogTail.from_dict(d.pop("stderr"))




        stdout = FailureLogTail.from_dict(d.pop("stdout"))




        storage = []
        _storage = d.pop("storage")
        for storage_item_data in (_storage):
            storage_item = FailureProperty.from_dict(storage_item_data)



            storage.append(storage_item)


        versions = []
        _versions = d.pop("versions")
        for versions_item_data in (_versions):
            versions_item = FailureProperty.from_dict(versions_item_data)



            versions.append(versions_item)


        schema_version = cast(Union[Literal[1], Unset] , d.pop("schema_version", UNSET))
        if schema_version != 1 and not isinstance(schema_version, Unset):
            raise ValueError(f"schema_version must match const 1, got '{schema_version}'")

        failure_diagnostics = cls(
            category=category,
            collected_at=collected_at,
            collector_errors=collector_errors,
            phase=phase,
            preflight=preflight,
            sandbox=sandbox,
            stderr=stderr,
            stdout=stdout,
            storage=storage,
            versions=versions,
            schema_version=schema_version,
        )

        return failure_diagnostics
