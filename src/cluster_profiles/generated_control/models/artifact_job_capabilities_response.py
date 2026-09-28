from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from typing import cast

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
     """

    storage: ArtifactJobStorageCapabilities
    transport: ArtifactJobTransportCapabilities





    def to_dict(self) -> dict[str, Any]:
        from ..models.artifact_job_storage_capabilities import ArtifactJobStorageCapabilities # noqa: PLC0415
        from ..models.artifact_job_transport_capabilities import ArtifactJobTransportCapabilities # noqa: PLC0415
        storage = self.storage.to_dict()

        transport = self.transport.to_dict()


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "storage": storage,
            "transport": transport,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.artifact_job_storage_capabilities import ArtifactJobStorageCapabilities # noqa: PLC0415
        from ..models.artifact_job_transport_capabilities import ArtifactJobTransportCapabilities # noqa: PLC0415
        d = dict(src_dict)
        storage = ArtifactJobStorageCapabilities.from_dict(d.pop("storage"))




        transport = ArtifactJobTransportCapabilities.from_dict(d.pop("transport"))




        artifact_job_capabilities_response = cls(
            storage=storage,
            transport=transport,
        )

        return artifact_job_capabilities_response
