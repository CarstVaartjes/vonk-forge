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
  from ..models.fleet_profile_verification_result import FleetProfileVerificationResult
  from ..models.fleet_profile_switch_adapter_result import FleetProfileSwitchAdapterResult
  from ..models.fleet_profile_switch_child_result import FleetProfileSwitchChildResult





T = TypeVar("T", bound="FleetProfileStepResult")



@_attrs_define
class FleetProfileStepResult:
    """ Result receipt for one completed profile plan step.

        Attributes:
            kind (Literal['switch']):
            operation_id (str):
            result (Union['FleetProfileSwitchAdapterResult', 'FleetProfileSwitchChildResult',
                'FleetProfileVerificationResult', None, Unset]):
     """

    kind: Literal['switch']
    operation_id: str
    result: Union['FleetProfileSwitchAdapterResult', 'FleetProfileSwitchChildResult', 'FleetProfileVerificationResult', None, Unset] = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.fleet_profile_verification_result import FleetProfileVerificationResult
        from ..models.fleet_profile_switch_adapter_result import FleetProfileSwitchAdapterResult
        from ..models.fleet_profile_switch_child_result import FleetProfileSwitchChildResult
        kind = self.kind

        operation_id = self.operation_id

        result: Union[None, Unset, dict[str, Any]]
        if isinstance(self.result, Unset):
            result = UNSET
        elif isinstance(self.result, FleetProfileSwitchChildResult):
            result = self.result.to_dict()
        elif isinstance(self.result, FleetProfileSwitchAdapterResult):
            result = self.result.to_dict()
        elif isinstance(self.result, FleetProfileVerificationResult):
            result = self.result.to_dict()
        else:
            result = self.result


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "kind": kind,
            "operation_id": operation_id,
        })
        if result is not UNSET:
            field_dict["result"] = result

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.fleet_profile_verification_result import FleetProfileVerificationResult
        from ..models.fleet_profile_switch_adapter_result import FleetProfileSwitchAdapterResult
        from ..models.fleet_profile_switch_child_result import FleetProfileSwitchChildResult
        d = dict(src_dict)
        kind = cast(Literal['switch'] , d.pop("kind"))
        if kind != 'switch':
            raise ValueError(f"kind must match const 'switch', got '{kind}'")

        operation_id = d.pop("operation_id")

        def _parse_result(data: object) -> Union['FleetProfileSwitchAdapterResult', 'FleetProfileSwitchChildResult', 'FleetProfileVerificationResult', None, Unset]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                result_type_0 = FleetProfileSwitchChildResult.from_dict(data)



                return result_type_0
            except: # noqa: E722
                pass
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                result_type_1 = FleetProfileSwitchAdapterResult.from_dict(data)



                return result_type_1
            except: # noqa: E722
                pass
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                result_type_2 = FleetProfileVerificationResult.from_dict(data)



                return result_type_2
            except: # noqa: E722
                pass
            return cast(Union['FleetProfileSwitchAdapterResult', 'FleetProfileSwitchChildResult', 'FleetProfileVerificationResult', None, Unset], data)

        result = _parse_result(d.pop("result", UNSET))


        fleet_profile_step_result = cls(
            kind=kind,
            operation_id=operation_id,
            result=result,
        )

        return fleet_profile_step_result
