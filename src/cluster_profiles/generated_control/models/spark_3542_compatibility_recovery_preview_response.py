from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.spark_3542_compatibility_recovery_preview_response_state import check_spark_3542_compatibility_recovery_preview_response_state
from ..models.spark_3542_compatibility_recovery_preview_response_state import Spark3542CompatibilityRecoveryPreviewResponseState
from typing import cast
from typing import cast, Union
from typing import Literal, cast

if TYPE_CHECKING:
  from ..models.spark_3542_compatibility_recovery_source_identity import Spark3542CompatibilityRecoverySourceIdentity
  from ..models.spark_3542_compatibility_recovery_target import Spark3542CompatibilityRecoveryTarget





T = TypeVar("T", bound="Spark3542CompatibilityRecoveryPreviewResponse")



@_attrs_define
class Spark3542CompatibilityRecoveryPreviewResponse:
    """
        Attributes:
            action (Literal['schedule-reboot']):
            authority_revision (str):
            compatibility_recovery_id (Literal['spark3542-a122-scheduled-reboot-v1']):
            delay_seconds (Literal[60]):
            dispatch_certificate_serial (str):
            dispatch_job_targets (list[Literal['spk_2818d189042b4c77aefa7796f4befd23']]):
            expected_retry_attempt (Literal[4]):
            job_id (Literal['6b945136-1be6-47e4-8ba0-5c5f815304ad']):
            node_id (Literal['spk_2818d189042b4c77aefa7796f4befd23']):
            operation_id (Literal['d54e0b56-e465-41bd-9627-c81f37352dfd']):
            plan_digest (str):
            required_confirmation (Literal['reboot-spark3542-to-resume-staged-a122-recovery']):
            source_attempt (Literal[3]):
            source_certificate_serial (str):
            source_fence (str):
            source_identity (Spark3542CompatibilityRecoverySourceIdentity):
            source_job_targets (list[Union[Literal['spk_2818d189042b4c77aefa7796f4befd23'],
                Literal['spk_9a86fdbab116442ab6707bf4181a3c1c']]]):
            state (Spark3542CompatibilityRecoveryPreviewResponseState):
            target (Spark3542CompatibilityRecoveryTarget):
            upgrade_payload_sha256 (str):
     """

    action: Literal['schedule-reboot']
    authority_revision: str
    compatibility_recovery_id: Literal['spark3542-a122-scheduled-reboot-v1']
    delay_seconds: Literal[60]
    dispatch_certificate_serial: str
    dispatch_job_targets: list[Literal['spk_2818d189042b4c77aefa7796f4befd23']]
    expected_retry_attempt: Literal[4]
    job_id: Literal['6b945136-1be6-47e4-8ba0-5c5f815304ad']
    node_id: Literal['spk_2818d189042b4c77aefa7796f4befd23']
    operation_id: Literal['d54e0b56-e465-41bd-9627-c81f37352dfd']
    plan_digest: str
    required_confirmation: Literal['reboot-spark3542-to-resume-staged-a122-recovery']
    source_attempt: Literal[3]
    source_certificate_serial: str
    source_fence: str
    source_identity: 'Spark3542CompatibilityRecoverySourceIdentity'
    source_job_targets: list[Union[Literal['spk_2818d189042b4c77aefa7796f4befd23'], Literal['spk_9a86fdbab116442ab6707bf4181a3c1c']]]
    state: Spark3542CompatibilityRecoveryPreviewResponseState
    target: 'Spark3542CompatibilityRecoveryTarget'
    upgrade_payload_sha256: str





    def to_dict(self) -> dict[str, Any]:
        from ..models.spark_3542_compatibility_recovery_source_identity import Spark3542CompatibilityRecoverySourceIdentity
        from ..models.spark_3542_compatibility_recovery_target import Spark3542CompatibilityRecoveryTarget
        action = self.action

        authority_revision = self.authority_revision

        compatibility_recovery_id = self.compatibility_recovery_id

        delay_seconds = self.delay_seconds

        dispatch_certificate_serial = self.dispatch_certificate_serial

        dispatch_job_targets = self.dispatch_job_targets



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

        source_job_targets = []
        for source_job_targets_item_data in self.source_job_targets:
            source_job_targets_item: Union[Literal['spk_2818d189042b4c77aefa7796f4befd23'], Literal['spk_9a86fdbab116442ab6707bf4181a3c1c']]
            source_job_targets_item = source_job_targets_item_data
            source_job_targets.append(source_job_targets_item)



        state: str = self.state

        target = self.target.to_dict()

        upgrade_payload_sha256 = self.upgrade_payload_sha256


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "action": action,
            "authority_revision": authority_revision,
            "compatibility_recovery_id": compatibility_recovery_id,
            "delay_seconds": delay_seconds,
            "dispatch_certificate_serial": dispatch_certificate_serial,
            "dispatch_job_targets": dispatch_job_targets,
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
            "source_job_targets": source_job_targets,
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
        action = cast(Literal['schedule-reboot'] , d.pop("action"))
        if action != 'schedule-reboot':
            raise ValueError(f"action must match const 'schedule-reboot', got '{action}'")

        authority_revision = d.pop("authority_revision")

        compatibility_recovery_id = cast(Literal['spark3542-a122-scheduled-reboot-v1'] , d.pop("compatibility_recovery_id"))
        if compatibility_recovery_id != 'spark3542-a122-scheduled-reboot-v1':
            raise ValueError(f"compatibility_recovery_id must match const 'spark3542-a122-scheduled-reboot-v1', got '{compatibility_recovery_id}'")

        delay_seconds = cast(Literal[60] , d.pop("delay_seconds"))
        if delay_seconds != 60:
            raise ValueError(f"delay_seconds must match const 60, got '{delay_seconds}'")

        dispatch_certificate_serial = d.pop("dispatch_certificate_serial")

        dispatch_job_targets = []
        _dispatch_job_targets = d.pop("dispatch_job_targets")
        for dispatch_job_targets_item_data in (_dispatch_job_targets):
            dispatch_job_targets_item = cast(Literal['spk_2818d189042b4c77aefa7796f4befd23'] , dispatch_job_targets_item_data)
            if dispatch_job_targets_item != 'spk_2818d189042b4c77aefa7796f4befd23':
                raise ValueError(f"dispatch_job_targets_item must match const 'spk_2818d189042b4c77aefa7796f4befd23', got '{dispatch_job_targets_item}'")
            dispatch_job_targets.append(dispatch_job_targets_item)


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

        required_confirmation = cast(Literal['reboot-spark3542-to-resume-staged-a122-recovery'] , d.pop("required_confirmation"))
        if required_confirmation != 'reboot-spark3542-to-resume-staged-a122-recovery':
            raise ValueError(f"required_confirmation must match const 'reboot-spark3542-to-resume-staged-a122-recovery', got '{required_confirmation}'")

        source_attempt = cast(Literal[3] , d.pop("source_attempt"))
        if source_attempt != 3:
            raise ValueError(f"source_attempt must match const 3, got '{source_attempt}'")

        source_certificate_serial = d.pop("source_certificate_serial")

        source_fence = d.pop("source_fence")

        source_identity = Spark3542CompatibilityRecoverySourceIdentity.from_dict(d.pop("source_identity"))




        source_job_targets = []
        _source_job_targets = d.pop("source_job_targets")
        for source_job_targets_item_data in (_source_job_targets):
            def _parse_source_job_targets_item(data: object) -> Union[Literal['spk_2818d189042b4c77aefa7796f4befd23'], Literal['spk_9a86fdbab116442ab6707bf4181a3c1c']]:
                source_job_targets_item_type_0 = cast(Literal['spk_2818d189042b4c77aefa7796f4befd23'] , data)
                if source_job_targets_item_type_0 != 'spk_2818d189042b4c77aefa7796f4befd23':
                    raise ValueError(f"source_job_targets_item_type_0 must match const 'spk_2818d189042b4c77aefa7796f4befd23', got '{source_job_targets_item_type_0}'")
                return source_job_targets_item_type_0
                source_job_targets_item_type_1 = cast(Literal['spk_9a86fdbab116442ab6707bf4181a3c1c'] , data)
                if source_job_targets_item_type_1 != 'spk_9a86fdbab116442ab6707bf4181a3c1c':
                    raise ValueError(f"source_job_targets_item_type_1 must match const 'spk_9a86fdbab116442ab6707bf4181a3c1c', got '{source_job_targets_item_type_1}'")
                return source_job_targets_item_type_1

            source_job_targets_item = _parse_source_job_targets_item(source_job_targets_item_data)

            source_job_targets.append(source_job_targets_item)


        state = check_spark_3542_compatibility_recovery_preview_response_state(d.pop("state"))




        target = Spark3542CompatibilityRecoveryTarget.from_dict(d.pop("target"))




        upgrade_payload_sha256 = d.pop("upgrade_payload_sha256")

        spark_3542_compatibility_recovery_preview_response = cls(
            action=action,
            authority_revision=authority_revision,
            compatibility_recovery_id=compatibility_recovery_id,
            delay_seconds=delay_seconds,
            dispatch_certificate_serial=dispatch_certificate_serial,
            dispatch_job_targets=dispatch_job_targets,
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
            source_job_targets=source_job_targets,
            state=state,
            target=target,
            upgrade_payload_sha256=upgrade_payload_sha256,
        )

        return spark_3542_compatibility_recovery_preview_response
