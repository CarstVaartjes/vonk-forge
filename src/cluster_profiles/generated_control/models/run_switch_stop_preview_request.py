from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
from typing import Literal, Union, cast
from typing import Union

if TYPE_CHECKING:
  from ..models.invocation_metadata import InvocationMetadata





T = TypeVar("T", bound="RunSwitchStopPreviewRequest")



@_attrs_define
class RunSwitchStopPreviewRequest:
    """
        Attributes:
            run_id (str):
            invocation (Union[Unset, InvocationMetadata]): Context for audit and tracing which has no decision-making
                authority.
            schema_version (Union[Literal[2], Unset]):  Default: 2.
     """

    run_id: str
    invocation: Union[Unset, 'InvocationMetadata'] = UNSET
    schema_version: Union[Literal[2], Unset] = 2





    def to_dict(self) -> dict[str, Any]:
        from ..models.invocation_metadata import InvocationMetadata
        run_id = self.run_id

        invocation: Union[Unset, dict[str, Any]] = UNSET
        if not isinstance(self.invocation, Unset):
            invocation = self.invocation.to_dict()

        schema_version = self.schema_version


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "run_id": run_id,
        })
        if invocation is not UNSET:
            field_dict["invocation"] = invocation
        if schema_version is not UNSET:
            field_dict["schema_version"] = schema_version

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.invocation_metadata import InvocationMetadata
        d = dict(src_dict)
        run_id = d.pop("run_id")

        _invocation = d.pop("invocation", UNSET)
        invocation: Union[Unset, InvocationMetadata]
        if isinstance(_invocation,  Unset):
            invocation = UNSET
        else:
            invocation = InvocationMetadata.from_dict(_invocation)




        schema_version = cast(Union[Literal[2], Unset] , d.pop("schema_version", UNSET))
        if schema_version != 2 and not isinstance(schema_version, Unset):
            raise ValueError(f"schema_version must match const 2, got '{schema_version}'")

        run_switch_stop_preview_request = cls(
            run_id=run_id,
            invocation=invocation,
            schema_version=schema_version,
        )

        return run_switch_stop_preview_request
