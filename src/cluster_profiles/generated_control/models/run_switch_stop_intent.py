from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
from typing import Literal, cast

if TYPE_CHECKING:
  from ..models.invocation_metadata import InvocationMetadata





T = TypeVar("T", bound="RunSwitchStopIntent")



@_attrs_define
class RunSwitchStopIntent:
    """
        Attributes:
            run_id (str):
            type_ (Literal['stop']):
            invocation (InvocationMetadata | Unset): Context for audit and tracing which has no decision-making authority.
            plan_digest (None | str | Unset):
            request_key (None | str | Unset):
            schema_version (Literal[2] | Unset):  Default: 2.
     """

    run_id: str
    type_: Literal['stop']
    invocation: InvocationMetadata | Unset = UNSET
    plan_digest: None | str | Unset = UNSET
    request_key: None | str | Unset = UNSET
    schema_version: Literal[2] | Unset = 2





    def to_dict(self) -> dict[str, Any]:
        from ..models.invocation_metadata import InvocationMetadata # noqa: PLC0415
        run_id = self.run_id

        type_ = self.type_

        invocation: dict[str, Any] | Unset = UNSET
        if not isinstance(self.invocation, Unset):
            invocation = self.invocation.to_dict()

        plan_digest: None | str | Unset
        if isinstance(self.plan_digest, Unset):
            plan_digest = UNSET
        else:
            plan_digest = self.plan_digest

        request_key: None | str | Unset
        if isinstance(self.request_key, Unset):
            request_key = UNSET
        else:
            request_key = self.request_key

        schema_version = self.schema_version


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "run_id": run_id,
            "type": type_,
        })
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
        from ..models.invocation_metadata import InvocationMetadata # noqa: PLC0415
        d = dict(src_dict)
        run_id = d.pop("run_id")

        type_ = cast(Literal['stop'] , d.pop("type"))
        if type_ != 'stop':
            raise ValueError(f"type must match const 'stop', got '{type_}'")

        _invocation = d.pop("invocation", UNSET)
        invocation: InvocationMetadata | Unset
        if isinstance(_invocation,  Unset):
            invocation = UNSET
        else:
            invocation = InvocationMetadata.from_dict(_invocation)




        def _parse_plan_digest(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        plan_digest = _parse_plan_digest(d.pop("plan_digest", UNSET))


        def _parse_request_key(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        request_key = _parse_request_key(d.pop("request_key", UNSET))


        schema_version = cast(Literal[2] | Unset , d.pop("schema_version", UNSET))
        if schema_version != 2 and not isinstance(schema_version, Unset):
            raise ValueError(f"schema_version must match const 2, got '{schema_version}'")

        run_switch_stop_intent = cls(
            run_id=run_id,
            type_=type_,
            invocation=invocation,
            plan_digest=plan_digest,
            request_key=request_key,
            schema_version=schema_version,
        )

        return run_switch_stop_intent
