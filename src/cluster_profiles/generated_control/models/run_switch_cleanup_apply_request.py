from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.run_switch_cleanup_apply_request_cleanup_mode import check_run_switch_cleanup_apply_request_cleanup_mode
from ..models.run_switch_cleanup_apply_request_cleanup_mode import RunSwitchCleanupApplyRequestCleanupMode
from ..types import UNSET, Unset
from typing import cast
from typing import cast, Union
from typing import Literal, Union, cast
from typing import Union

if TYPE_CHECKING:
  from ..models.invocation_metadata import InvocationMetadata





T = TypeVar("T", bound="RunSwitchCleanupApplyRequest")



@_attrs_define
class RunSwitchCleanupApplyRequest:
    """
        Attributes:
            installation_id (str):
            cleanup_mode (Union[Unset, RunSwitchCleanupApplyRequestCleanupMode]):  Default: 'uninstall'.
            invocation (Union[Unset, InvocationMetadata]): Context for audit and tracing which has no decision-making
                authority.
            plan_digest (Union[None, Unset, str]):
            request_key (Union[None, Unset, str]):
            schema_version (Union[Literal[2], Unset]):  Default: 2.
     """

    installation_id: str
    cleanup_mode: Union[Unset, RunSwitchCleanupApplyRequestCleanupMode] = 'uninstall'
    invocation: Union[Unset, 'InvocationMetadata'] = UNSET
    plan_digest: Union[None, Unset, str] = UNSET
    request_key: Union[None, Unset, str] = UNSET
    schema_version: Union[Literal[2], Unset] = 2





    def to_dict(self) -> dict[str, Any]:
        from ..models.invocation_metadata import InvocationMetadata
        installation_id = self.installation_id

        cleanup_mode: Union[Unset, str] = UNSET
        if not isinstance(self.cleanup_mode, Unset):
            cleanup_mode = self.cleanup_mode


        invocation: Union[Unset, dict[str, Any]] = UNSET
        if not isinstance(self.invocation, Unset):
            invocation = self.invocation.to_dict()

        plan_digest: Union[None, Unset, str]
        if isinstance(self.plan_digest, Unset):
            plan_digest = UNSET
        else:
            plan_digest = self.plan_digest

        request_key: Union[None, Unset, str]
        if isinstance(self.request_key, Unset):
            request_key = UNSET
        else:
            request_key = self.request_key

        schema_version = self.schema_version


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "installation_id": installation_id,
        })
        if cleanup_mode is not UNSET:
            field_dict["cleanup_mode"] = cleanup_mode
        if invocation is not UNSET:
            field_dict["invocation"] = invocation
        if plan_digest is not UNSET:
            field_dict["plan_digest"] = plan_digest
        if request_key is not UNSET:
            field_dict["request_key"] = request_key
        if schema_version is not UNSET:
            field_dict["schema_version"] = schema_version

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.invocation_metadata import InvocationMetadata
        d = dict(src_dict)
        installation_id = d.pop("installation_id")

        _cleanup_mode = d.pop("cleanup_mode", UNSET)
        cleanup_mode: Union[Unset, RunSwitchCleanupApplyRequestCleanupMode]
        if isinstance(_cleanup_mode,  Unset):
            cleanup_mode = UNSET
        else:
            cleanup_mode = check_run_switch_cleanup_apply_request_cleanup_mode(_cleanup_mode)




        _invocation = d.pop("invocation", UNSET)
        invocation: Union[Unset, InvocationMetadata]
        if isinstance(_invocation,  Unset):
            invocation = UNSET
        else:
            invocation = InvocationMetadata.from_dict(_invocation)




        def _parse_plan_digest(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        plan_digest = _parse_plan_digest(d.pop("plan_digest", UNSET))


        def _parse_request_key(data: object) -> Union[None, Unset, str]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, str], data)

        request_key = _parse_request_key(d.pop("request_key", UNSET))


        schema_version = cast(Union[Literal[2], Unset] , d.pop("schema_version", UNSET))
        if schema_version != 2 and not isinstance(schema_version, Unset):
            raise ValueError(f"schema_version must match const 2, got '{schema_version}'")

        run_switch_cleanup_apply_request = cls(
            installation_id=installation_id,
            cleanup_mode=cleanup_mode,
            invocation=invocation,
            plan_digest=plan_digest,
            request_key=request_key,
            schema_version=schema_version,
        )

        return run_switch_cleanup_apply_request
