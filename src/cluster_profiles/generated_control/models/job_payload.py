from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import Literal, cast
from typing import Literal, Union, cast






T = TypeVar("T", bound="JobPayload")



@_attrs_define
class JobPayload:
    """
        Attributes:
            entity_id (str):
            entity_kind (Literal['job']):
            kind (str):
            state (str):
            target_count (int):
            schema_version (Union[Literal[1], Unset]):  Default: 1.
     """

    entity_id: str
    entity_kind: Literal['job']
    kind: str
    state: str
    target_count: int
    schema_version: Union[Literal[1], Unset] = 1





    def to_dict(self) -> dict[str, Any]:
        entity_id = self.entity_id

        entity_kind = self.entity_kind

        kind = self.kind

        state = self.state

        target_count = self.target_count

        schema_version = self.schema_version


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "entity_id": entity_id,
            "entity_kind": entity_kind,
            "kind": kind,
            "state": state,
            "target_count": target_count,
        })
        if schema_version is not UNSET:
            field_dict["schema_version"] = schema_version

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        entity_id = d.pop("entity_id")

        entity_kind = cast(Literal['job'] , d.pop("entity_kind"))
        if entity_kind != 'job':
            raise ValueError(f"entity_kind must match const 'job', got '{entity_kind}'")

        kind = d.pop("kind")

        state = d.pop("state")

        target_count = d.pop("target_count")

        schema_version = cast(Union[Literal[1], Unset] , d.pop("schema_version", UNSET))
        if schema_version != 1 and not isinstance(schema_version, Unset):
            raise ValueError(f"schema_version must match const 1, got '{schema_version}'")

        job_payload = cls(
            entity_id=entity_id,
            entity_kind=entity_kind,
            kind=kind,
            state=state,
            target_count=target_count,
            schema_version=schema_version,
        )

        return job_payload
