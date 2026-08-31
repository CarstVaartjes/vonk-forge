from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.spark_3542_compatibility_recovery_preview_response_state import check_spark_3542_compatibility_recovery_preview_response_state
from ..models.spark_3542_compatibility_recovery_preview_response_state import Spark3542CompatibilityRecoveryPreviewResponseState
from typing import cast
from typing import Literal, cast

if TYPE_CHECKING:
  from ..models.spark_3542_compatibility_recovery_source_identity import Spark3542CompatibilityRecoverySourceIdentity
  from ..models.spark_3542_compatibility_recovery_target import Spark3542CompatibilityRecoveryTarget





T = TypeVar("T", bound="Spark3542CompatibilityRecoveryPreviewResponse")



@_attrs_define
class Spark3542CompatibilityRecoveryPreviewResponse:
    """
        Attributes:
            action (Literal['retry-exact-package-install']):
            authority_revision (str):
            compatibility_recovery_id (Literal['spark3542-a122-exact-package-retry-v1']):
            expected_retry_attempt (Literal[4]):
            job_id (Literal['6b945136-1be6-47e4-8ba0-5c5f815304ad']):
            node_id (Literal['spk_2818d189042b4c77aefa7796f4befd23']):
            operation_id (Literal['d54e0b56-e465-41bd-9627-c81f37352dfd']):
            plan_digest (str):
            required_confirmation (Literal['retry-exact-staged-a122-package-on-spark3542']):
            source_attempt (Literal[3]):
            source_certificate_serial (str):
            source_fence (str):
            source_identity (Spark3542CompatibilityRecoverySourceIdentity):
            state (Spark3542CompatibilityRecoveryPreviewResponseState):
            target (Spark3542CompatibilityRecoveryTarget):
            upgrade_payload_sha256 (str):
     """

    action: Literal['retry-exact-package-install']
    authority_revision: str
    compatibility_recovery_id: Literal['spark3542-a122-exact-package-retry-v1']
    expected_retry_attempt: Literal[4]
    job_id: Literal['6b945136-1be6-47e4-8ba0-5c5f815304ad']
    node_id: Literal['spk_2818d189042b4c77aefa7796f4befd23']
    operation_id: Literal['d54e0b56-e465-41bd-9627-c81f37352dfd']
    plan_digest: str
    required_confirmation: Literal['retry-exact-staged-a122-package-on-spark3542']
    source_attempt: Literal[3]
    source_certificate_serial: str
    source_fence: str
    source_identity: 'Spark3542CompatibilityRecoverySourceIdentity'
    state: Spark3542CompatibilityRecoveryPreviewResponseState
    target: 'Spark3542CompatibilityRecoveryTarget'
    upgrade_payload_sha256: str





    def to_dict(self) -> dict[str, Any]:
        from ..models.spark_3542_compatibility_recovery_source_identity import Spark3542CompatibilityRecoverySourceIdentity
        from ..models.spark_3542_compatibility_recovery_target import Spark3542CompatibilityRecoveryTarget
        action = self.action

        authority_revision = self.authority_revision

        compatibility_recovery_id = self.compatibility_recovery_id

        expected_retry_attempt = self.expected_retry_attempt

        job_id = self.job_id

        node_id = self.node_id

        operation_id = self.operation_id

        plan_digest = self.plan_digest

        required_confirmation = self.required_confirmation

        source_attempt = self.source_attempt

        source_certificate_serial = self.source_certificate_serial

        source_fence = self.source_fence

        source_identity = self.source_identity.to_dict()

        state: str = self.state

        target = self.target.to_dict()

        upgrade_payload_sha256 = self.upgrade_payload_sha256


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "action": action,
            "authority_revision": authority_revision,
            "compatibility_recovery_id": compatibility_recovery_id,
            "expected_retry_attempt": expected_retry_attempt,
            "job_id": job_id,
            "node_id": node_id,
            "operation_id": operation_id,
            "plan_digest": plan_digest,
            "required_confirmation": required_confirmation,
            "source_attempt": source_attempt,
            "source_certificate_serial": source_certificate_serial,
            "source_fence": source_fence,
            "source_identity": source_identity,
            "state": state,
            "target": target,
            "upgrade_payload_sha256": upgrade_payload_sha256,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.spark_3542_compatibility_recovery_source_identity import Spark3542CompatibilityRecoverySourceIdentity
        from ..models.spark_3542_compatibility_recovery_target import Spark3542CompatibilityRecoveryTarget
        d = dict(src_dict)
        action = cast(Literal['retry-exact-package-install'] , d.pop("action"))
        if action != 'retry-exact-package-install':
            raise ValueError(f"action must match const 'retry-exact-package-install', got '{action}'")

        authority_revision = d.pop("authority_revision")

        compatibility_recovery_id = cast(Literal['spark3542-a122-exact-package-retry-v1'] , d.pop("compatibility_recovery_id"))
        if compatibility_recovery_id != 'spark3542-a122-exact-package-retry-v1':
            raise ValueError(f"compatibility_recovery_id must match const 'spark3542-a122-exact-package-retry-v1', got '{compatibility_recovery_id}'")

        expected_retry_attempt = cast(Literal[4] , d.pop("expected_retry_attempt"))
        if expected_retry_attempt != 4:
            raise ValueError(f"expected_retry_attempt must match const 4, got '{expected_retry_attempt}'")

        job_id = cast(Literal['6b945136-1be6-47e4-8ba0-5c5f815304ad'] , d.pop("job_id"))
        if job_id != '6b945136-1be6-47e4-8ba0-5c5f815304ad':
            raise ValueError(f"job_id must match const '6b945136-1be6-47e4-8ba0-5c5f815304ad', got '{job_id}'")

        node_id = cast(Literal['spk_2818d189042b4c77aefa7796f4befd23'] , d.pop("node_id"))
        if node_id != 'spk_2818d189042b4c77aefa7796f4befd23':
            raise ValueError(f"node_id must match const 'spk_2818d189042b4c77aefa7796f4befd23', got '{node_id}'")

        operation_id = cast(Literal['d54e0b56-e465-41bd-9627-c81f37352dfd'] , d.pop("operation_id"))
        if operation_id != 'd54e0b56-e465-41bd-9627-c81f37352dfd':
            raise ValueError(f"operation_id must match const 'd54e0b56-e465-41bd-9627-c81f37352dfd', got '{operation_id}'")

        plan_digest = d.pop("plan_digest")

        required_confirmation = cast(Literal['retry-exact-staged-a122-package-on-spark3542'] , d.pop("required_confirmation"))
        if required_confirmation != 'retry-exact-staged-a122-package-on-spark3542':
            raise ValueError(f"required_confirmation must match const 'retry-exact-staged-a122-package-on-spark3542', got '{required_confirmation}'")

        source_attempt = cast(Literal[3] , d.pop("source_attempt"))
        if source_attempt != 3:
            raise ValueError(f"source_attempt must match const 3, got '{source_attempt}'")

        source_certificate_serial = d.pop("source_certificate_serial")

        source_fence = d.pop("source_fence")

        source_identity = Spark3542CompatibilityRecoverySourceIdentity.from_dict(d.pop("source_identity"))




        state = check_spark_3542_compatibility_recovery_preview_response_state(d.pop("state"))




        target = Spark3542CompatibilityRecoveryTarget.from_dict(d.pop("target"))




        upgrade_payload_sha256 = d.pop("upgrade_payload_sha256")

        spark_3542_compatibility_recovery_preview_response = cls(
            action=action,
            authority_revision=authority_revision,
            compatibility_recovery_id=compatibility_recovery_id,
            expected_retry_attempt=expected_retry_attempt,
            job_id=job_id,
            node_id=node_id,
            operation_id=operation_id,
            plan_digest=plan_digest,
            required_confirmation=required_confirmation,
            source_attempt=source_attempt,
            source_certificate_serial=source_certificate_serial,
            source_fence=source_fence,
            source_identity=source_identity,
            state=state,
            target=target,
            upgrade_payload_sha256=upgrade_payload_sha256,
        )

        return spark_3542_compatibility_recovery_preview_response
