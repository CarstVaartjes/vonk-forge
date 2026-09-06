from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast
from typing import Literal, Union, cast

if TYPE_CHECKING:
  from ..models.artifact_job_storage_capabilities import ArtifactJobStorageCapabilities
  from ..models.artifact_job_transport_capabilities import ArtifactJobTransportCapabilities





T = TypeVar("T", bound="ArtifactJobCapabilitiesResponse")



@_attrs_define
class ArtifactJobCapabilitiesResponse:
    """
        Attributes:
            storage (ArtifactJobStorageCapabilities):
            transport (ArtifactJobTransportCapabilities):
            schema_version (Union[Literal[1], Unset]):  Default: 1.
     """

    storage: 'ArtifactJobStorageCapabilities'
    transport: 'ArtifactJobTransportCapabilities'
    schema_version: Union[Literal[1], Unset] = 1





    def to_dict(self) -> dict[str, Any]:
        from ..models.artifact_job_storage_capabilities import ArtifactJobStorageCapabilities
        from ..models.artifact_job_transport_capabilities import ArtifactJobTransportCapabilities
        storage = self.storage.to_dict()

        transport = self.transport.to_dict()

        schema_version = self.schema_version


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "storage": storage,
            "transport": transport,
        })
        if schema_version is not UNSET:
            field_dict["schema_version"] = schema_version

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.artifact_job_storage_capabilities import ArtifactJobStorageCapabilities
        from ..models.artifact_job_transport_capabilities import ArtifactJobTransportCapabilities
        d = dict(src_dict)
        storage = ArtifactJobStorageCapabilities.from_dict(d.pop("storage"))




        transport = ArtifactJobTransportCapabilities.from_dict(d.pop("transport"))




        schema_version = cast(Union[Literal[1], Unset] , d.pop("schema_version", UNSET))
        if schema_version != 1 and not isinstance(schema_version, Unset):
            raise ValueError(f"schema_version must match const 1, got '{schema_version}'")

        artifact_job_capabilities_response = cls(
            storage=storage,
            transport=transport,
            schema_version=schema_version,
        )

        return artifact_job_capabilities_response
