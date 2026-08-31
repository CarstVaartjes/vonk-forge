from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset







T = TypeVar("T", bound="Spark3542CompatibilityRecoveryQueuedMutation")



@_attrs_define
class Spark3542CompatibilityRecoveryQueuedMutation:
    """
        Attributes:
            authority_revision (str):
            job_id (str):
            kind (str):
            operation_id (str):
            payload_digest (str):
     """

    authority_revision: str
    job_id: str
    kind: str
    operation_id: str
    payload_digest: str





    def to_dict(self) -> dict[str, Any]:
        authority_revision = self.authority_revision

        job_id = self.job_id

        kind = self.kind

        operation_id = self.operation_id

        payload_digest = self.payload_digest


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "authority_revision": authority_revision,
            "job_id": job_id,
            "kind": kind,
            "operation_id": operation_id,
            "payload_digest": payload_digest,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        authority_revision = d.pop("authority_revision")

        job_id = d.pop("job_id")

        kind = d.pop("kind")

        operation_id = d.pop("operation_id")

        payload_digest = d.pop("payload_digest")

        spark_3542_compatibility_recovery_queued_mutation = cls(
            authority_revision=authority_revision,
            job_id=job_id,
            kind=kind,
            operation_id=operation_id,
            payload_digest=payload_digest,
        )

        return spark_3542_compatibility_recovery_queued_mutation
