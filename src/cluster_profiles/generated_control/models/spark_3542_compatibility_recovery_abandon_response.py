from collections.abc import Mapping
from typing import Any, TypeVar, Optional, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.spark_3542_compatibility_recovery_abandon_response_grant_disposition import check_spark_3542_compatibility_recovery_abandon_response_grant_disposition
from ..models.spark_3542_compatibility_recovery_abandon_response_grant_disposition import Spark3542CompatibilityRecoveryAbandonResponseGrantDisposition
from ..models.spark_3542_compatibility_recovery_abandon_response_state import check_spark_3542_compatibility_recovery_abandon_response_state
from ..models.spark_3542_compatibility_recovery_abandon_response_state import Spark3542CompatibilityRecoveryAbandonResponseState
from dateutil.parser import isoparse
from typing import cast
from typing import cast, Union
from typing import Literal, cast
import datetime

if TYPE_CHECKING:
  from ..models.spark_3542_compatibility_recovery_source_identity import Spark3542CompatibilityRecoverySourceIdentity
  from ..models.spark_3542_compatibility_recovery_queued_mutation import Spark3542CompatibilityRecoveryQueuedMutation





T = TypeVar("T", bound="Spark3542CompatibilityRecoveryAbandonResponse")



@_attrs_define
class Spark3542CompatibilityRecoveryAbandonResponse:
    """
        Attributes:
            action (Literal['abandon-recovery']):
            blocked_at (datetime.datetime):
            compatibility_recovery_id (Literal['spark3542-a122-scheduled-reboot-v1']):
            contact_certificate_serial (str):
            grant_disposition (Spark3542CompatibilityRecoveryAbandonResponseGrantDisposition):
            identity_deadline (Union[None, datetime.datetime]):
            job_id (Literal['6b945136-1be6-47e4-8ba0-5c5f815304ad']):
            node_id (Literal['spk_2818d189042b4c77aefa7796f4befd23']):
            operation_id (Literal['d54e0b56-e465-41bd-9627-c81f37352dfd']):
            plan_digest (str):
            queued_mutations (list['Spark3542CompatibilityRecoveryQueuedMutation']):
            retry_attempt (Literal[4]):
            source_identity (Spark3542CompatibilityRecoverySourceIdentity):
            state (Spark3542CompatibilityRecoveryAbandonResponseState):
     """

    action: Literal['abandon-recovery']
    blocked_at: datetime.datetime
    compatibility_recovery_id: Literal['spark3542-a122-scheduled-reboot-v1']
    contact_certificate_serial: str
    grant_disposition: Spark3542CompatibilityRecoveryAbandonResponseGrantDisposition
    identity_deadline: Union[None, datetime.datetime]
    job_id: Literal['6b945136-1be6-47e4-8ba0-5c5f815304ad']
    node_id: Literal['spk_2818d189042b4c77aefa7796f4befd23']
    operation_id: Literal['d54e0b56-e465-41bd-9627-c81f37352dfd']
    plan_digest: str
    queued_mutations: list['Spark3542CompatibilityRecoveryQueuedMutation']
    retry_attempt: Literal[4]
    source_identity: 'Spark3542CompatibilityRecoverySourceIdentity'
    state: Spark3542CompatibilityRecoveryAbandonResponseState





    def to_dict(self) -> dict[str, Any]:
        from ..models.spark_3542_compatibility_recovery_source_identity import Spark3542CompatibilityRecoverySourceIdentity
        from ..models.spark_3542_compatibility_recovery_queued_mutation import Spark3542CompatibilityRecoveryQueuedMutation
        action = self.action

        blocked_at = self.blocked_at.isoformat()

        compatibility_recovery_id = self.compatibility_recovery_id

        contact_certificate_serial = self.contact_certificate_serial

        grant_disposition: str = self.grant_disposition

        identity_deadline: Union[None, str]
        if isinstance(self.identity_deadline, datetime.datetime):
            identity_deadline = self.identity_deadline.isoformat()
        else:
            identity_deadline = self.identity_deadline

        job_id = self.job_id

        node_id = self.node_id

        operation_id = self.operation_id

        plan_digest = self.plan_digest

        queued_mutations = []
        for queued_mutations_item_data in self.queued_mutations:
            queued_mutations_item = queued_mutations_item_data.to_dict()
            queued_mutations.append(queued_mutations_item)



        retry_attempt = self.retry_attempt

        source_identity = self.source_identity.to_dict()

        state: str = self.state


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "action": action,
            "blocked_at": blocked_at,
            "compatibility_recovery_id": compatibility_recovery_id,
            "contact_certificate_serial": contact_certificate_serial,
            "grant_disposition": grant_disposition,
            "identity_deadline": identity_deadline,
            "job_id": job_id,
            "node_id": node_id,
            "operation_id": operation_id,
            "plan_digest": plan_digest,
            "queued_mutations": queued_mutations,
            "retry_attempt": retry_attempt,
            "source_identity": source_identity,
            "state": state,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        from ..models.spark_3542_compatibility_recovery_source_identity import Spark3542CompatibilityRecoverySourceIdentity
        from ..models.spark_3542_compatibility_recovery_queued_mutation import Spark3542CompatibilityRecoveryQueuedMutation
        d = dict(src_dict)
        action = cast(Literal['abandon-recovery'] , d.pop("action"))
        if action != 'abandon-recovery':
            raise ValueError(f"action must match const 'abandon-recovery', got '{action}'")

        blocked_at = isoparse(d.pop("blocked_at"))




        compatibility_recovery_id = cast(Literal['spark3542-a122-scheduled-reboot-v1'] , d.pop("compatibility_recovery_id"))
        if compatibility_recovery_id != 'spark3542-a122-scheduled-reboot-v1':
            raise ValueError(f"compatibility_recovery_id must match const 'spark3542-a122-scheduled-reboot-v1', got '{compatibility_recovery_id}'")

        contact_certificate_serial = d.pop("contact_certificate_serial")

        grant_disposition = check_spark_3542_compatibility_recovery_abandon_response_grant_disposition(d.pop("grant_disposition"))




        def _parse_identity_deadline(data: object) -> Union[None, datetime.datetime]:
            if data is None:
                return data
            try:
                if not isinstance(data, str):
                    raise TypeError()
                identity_deadline_type_0 = isoparse(data)



                return identity_deadline_type_0
            except: # noqa: E722
                pass
            return cast(Union[None, datetime.datetime], data)

        identity_deadline = _parse_identity_deadline(d.pop("identity_deadline"))


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

        queued_mutations = []
        _queued_mutations = d.pop("queued_mutations")
        for queued_mutations_item_data in (_queued_mutations):
            queued_mutations_item = Spark3542CompatibilityRecoveryQueuedMutation.from_dict(queued_mutations_item_data)



            queued_mutations.append(queued_mutations_item)


        retry_attempt = cast(Literal[4] , d.pop("retry_attempt"))
        if retry_attempt != 4:
            raise ValueError(f"retry_attempt must match const 4, got '{retry_attempt}'")

        source_identity = Spark3542CompatibilityRecoverySourceIdentity.from_dict(d.pop("source_identity"))




        state = check_spark_3542_compatibility_recovery_abandon_response_state(d.pop("state"))




        spark_3542_compatibility_recovery_abandon_response = cls(
            action=action,
            blocked_at=blocked_at,
            compatibility_recovery_id=compatibility_recovery_id,
            contact_certificate_serial=contact_certificate_serial,
            grant_disposition=grant_disposition,
            identity_deadline=identity_deadline,
            job_id=job_id,
            node_id=node_id,
            operation_id=operation_id,
            plan_digest=plan_digest,
            queued_mutations=queued_mutations,
            retry_attempt=retry_attempt,
            source_identity=source_identity,
            state=state,
        )

        return spark_3542_compatibility_recovery_abandon_response
