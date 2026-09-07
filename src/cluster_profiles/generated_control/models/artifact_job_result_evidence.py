from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast, Union
from typing import Union






T = TypeVar("T", bound="ArtifactJobResultEvidence")



@_attrs_define
class ArtifactJobResultEvidence:
    """ Known evidence fields with room for engine-specific evidence keys.

        Attributes:
            elapsed_milliseconds (Union[None, Unset, int]):
            peak_memory_bytes (Union[None, Unset, int]):
     """

    elapsed_milliseconds: Union[None, Unset, int] = UNSET
    peak_memory_bytes: Union[None, Unset, int] = UNSET
    additional_properties: dict[str, Any] = _attrs_field(init=False, factory=dict)





    def to_dict(self) -> dict[str, Any]:
        elapsed_milliseconds: Union[None, Unset, int]
        if isinstance(self.elapsed_milliseconds, Unset):
            elapsed_milliseconds = UNSET
        else:
            elapsed_milliseconds = self.elapsed_milliseconds

        peak_memory_bytes: Union[None, Unset, int]
        if isinstance(self.peak_memory_bytes, Unset):
            peak_memory_bytes = UNSET
        else:
            peak_memory_bytes = self.peak_memory_bytes


        field_dict: dict[str, Any] = {}
        field_dict.update(self.additional_properties)
        field_dict.update({
        })
        if elapsed_milliseconds is not UNSET:
            field_dict["elapsed_milliseconds"] = elapsed_milliseconds
        if peak_memory_bytes is not UNSET:
            field_dict["peak_memory_bytes"] = peak_memory_bytes

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        def _parse_elapsed_milliseconds(data: object) -> Union[None, Unset, int]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, int], data)

        elapsed_milliseconds = _parse_elapsed_milliseconds(d.pop("elapsed_milliseconds", UNSET))


        def _parse_peak_memory_bytes(data: object) -> Union[None, Unset, int]:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(Union[None, Unset, int], data)

        peak_memory_bytes = _parse_peak_memory_bytes(d.pop("peak_memory_bytes", UNSET))


        artifact_job_result_evidence = cls(
            elapsed_milliseconds=elapsed_milliseconds,
            peak_memory_bytes=peak_memory_bytes,
        )


        artifact_job_result_evidence.additional_properties = d
        return artifact_job_result_evidence

    @property
    def additional_keys(self) -> list[str]:
        return list(self.additional_properties.keys())

    def __getitem__(self, key: str) -> Any:
        return self.additional_properties[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self.additional_properties[key] = value

    def __delitem__(self, key: str) -> None:
        del self.additional_properties[key]

    def __contains__(self, key: str) -> bool:
        return key in self.additional_properties
