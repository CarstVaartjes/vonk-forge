from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.cache_artifact_response_state import CacheArtifactResponseState
from ..models.cache_artifact_response_state import check_cache_artifact_response_state
from ..types import UNSET, Unset
from typing import cast
from typing import Literal, Union, cast






T = TypeVar("T", bound="CacheArtifactResponse")



@_attrs_define
class CacheArtifactResponse:
    """
        Attributes:
            actual_bytes (int):
            expected_bytes (int):
            id (str):
            key (str):
            path (str):
            roles (list[str]):
            sha256 (str):
            source (str):
            state (CacheArtifactResponseState):
            schema_version (Union[Literal[2], Unset]):  Default: 2.
     """

    actual_bytes: int
    expected_bytes: int
    id: str
    key: str
    path: str
    roles: list[str]
    sha256: str
    source: str
    state: CacheArtifactResponseState
    schema_version: Union[Literal[2], Unset] = 2





    def to_dict(self) -> dict[str, Any]:
        actual_bytes = self.actual_bytes

        expected_bytes = self.expected_bytes

        id = self.id

        key = self.key

        path = self.path

        roles = self.roles



        sha256 = self.sha256

        source = self.source

        state: str = self.state

        schema_version = self.schema_version


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "actual_bytes": actual_bytes,
            "expected_bytes": expected_bytes,
            "id": id,
            "key": key,
            "path": path,
            "roles": roles,
            "sha256": sha256,
            "source": source,
            "state": state,
        })
        if schema_version is not UNSET:
            field_dict["schema_version"] = schema_version

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        actual_bytes = d.pop("actual_bytes")

        expected_bytes = d.pop("expected_bytes")

        id = d.pop("id")

        key = d.pop("key")

        path = d.pop("path")

        roles = cast(list[str], d.pop("roles"))


        sha256 = d.pop("sha256")

        source = d.pop("source")

        state = check_cache_artifact_response_state(d.pop("state"))




        schema_version = cast(Union[Literal[2], Unset] , d.pop("schema_version", UNSET))
        if schema_version != 2 and not isinstance(schema_version, Unset):
            raise ValueError(f"schema_version must match const 2, got '{schema_version}'")

        cache_artifact_response = cls(
            actual_bytes=actual_bytes,
            expected_bytes=expected_bytes,
            id=id,
            key=key,
            path=path,
            roles=roles,
            sha256=sha256,
            source=source,
            state=state,
            schema_version=schema_version,
        )

        return cache_artifact_response
