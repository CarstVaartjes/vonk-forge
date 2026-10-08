from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..types import UNSET, Unset
from typing import cast

if TYPE_CHECKING:
  from ..models.operation_progress import OperationProgress





T = TypeVar("T", bound="NativeProgressWitness")



@_attrs_define
class NativeProgressWitness:
    """
        Attributes:
            attempt_id (str):
            certificate_serial (str):
            fence (str):
            node_id (str):
            operation_id (str):
            payload_digest (str):
            sample (None | OperationProgress | Unset):
            sample_digest (None | str | Unset):
     """

    attempt_id: str
    certificate_serial: str
    fence: str
    node_id: str
    operation_id: str
    payload_digest: str
    sample: None | OperationProgress | Unset = UNSET
    sample_digest: None | str | Unset = UNSET





    def to_dict(self) -> dict[str, Any]:
        from ..models.operation_progress import OperationProgress # noqa: PLC0415
        attempt_id = self.attempt_id

        certificate_serial = self.certificate_serial

        fence = self.fence

        node_id = self.node_id

        operation_id = self.operation_id

        payload_digest = self.payload_digest

        sample: dict[str, Any] | None | Unset
        if isinstance(self.sample, Unset):
            sample = UNSET
        elif isinstance(self.sample, OperationProgress):
            sample = self.sample.to_dict()
        else:
            sample = self.sample

        sample_digest: None | str | Unset
        if isinstance(self.sample_digest, Unset):
            sample_digest = UNSET
        else:
            sample_digest = self.sample_digest


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "attempt_id": attempt_id,
            "certificate_serial": certificate_serial,
            "fence": fence,
            "node_id": node_id,
            "operation_id": operation_id,
            "payload_digest": payload_digest,
        })
        if sample is not UNSET:
            field_dict["sample"] = sample
        if sample_digest is not UNSET:
            field_dict["sample_digest"] = sample_digest

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.operation_progress import OperationProgress # noqa: PLC0415
        d = dict(src_dict)
        attempt_id = d.pop("attempt_id")

        certificate_serial = d.pop("certificate_serial")

        fence = d.pop("fence")

        node_id = d.pop("node_id")

        operation_id = d.pop("operation_id")

        payload_digest = d.pop("payload_digest")

        def _parse_sample(data: object) -> None | OperationProgress | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            try:
                if not isinstance(data, dict):
                    raise TypeError()
                sample_type_0 = OperationProgress.from_dict(data)



                return sample_type_0
            except (TypeError, ValueError, AttributeError, KeyError):
                pass
            return cast(None | OperationProgress | Unset, data)

        sample = _parse_sample(d.pop("sample", UNSET))


        def _parse_sample_digest(data: object) -> None | str | Unset:
            if data is None:
                return data
            if isinstance(data, Unset):
                return data
            return cast(None | str | Unset, data)

        sample_digest = _parse_sample_digest(d.pop("sample_digest", UNSET))


        native_progress_witness = cls(
            attempt_id=attempt_id,
            certificate_serial=certificate_serial,
            fence=fence,
            node_id=node_id,
            operation_id=operation_id,
            payload_digest=payload_digest,
            sample=sample,
            sample_digest=sample_digest,
        )

        return native_progress_witness
