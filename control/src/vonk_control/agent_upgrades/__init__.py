"""Public exports for agent upgrades."""

import hashlib as hashlib
import re as re
import secrets as secrets
import uuid as uuid
from collections.abc import (
    Callable as Callable,
)
from collections.abc import (
    Mapping as Mapping,
)
from collections.abc import (
    Sequence as Sequence,
)
from dataclasses import (
    dataclass as dataclass,
)
from datetime import (
    UTC as UTC,
)
from datetime import (
    datetime as datetime,
)
from datetime import (
    timedelta as timedelta,
)

import httpx2 as httpx2
from pydantic import (
    BaseModel as BaseModel,
)
from sqlalchemy import (
    and_ as and_,
)
from sqlalchemy import (
    or_ as or_,
)
from sqlalchemy import (
    select as select,
)
from sqlalchemy.exc import (
    IntegrityError as IntegrityError,
)
from sqlalchemy.orm import (
    Session as Session,
)
from sqlalchemy.orm import (
    sessionmaker as sessionmaker,
)
from vonk_agent_protocol import (
    AgentResult as AgentResult,
)
from vonk_agent_protocol import (
    InvalidRequestError as InvalidRequestError,
)
from vonk_agent_protocol import (
    InvalidRequestReason as InvalidRequestReason,
)
from vonk_agent_protocol import (
    LifecycleState as LifecycleState,
)
from vonk_agent_protocol import (
    SecurityRefusalError as SecurityRefusalError,
)
from vonk_agent_protocol import (
    SecurityRefusalReason as SecurityRefusalReason,
)
from vonk_agent_protocol import (
    UnknownOutcomeError as UnknownOutcomeError,
)
from vonk_agent_protocol import (
    WaitReason as WaitReason,
)
from vonk_agent_protocol import (
    canonical_message as canonical_message,
)
from vonk_agent_protocol.claims import (
    AGENT_PROTOCOL_VERSION as AGENT_PROTOCOL_VERSION,
)
from vonk_agent_protocol.contracts import (
    AgentUpgradePayload as AgentUpgradePayload,
)
from vonk_agent_protocol.package_source import (
    AgentPackageSource as AgentPackageSource,
)
from vonk_agent_protocol.package_upgrade import (
    PackageRollbackAuthority as PackageRollbackAuthority,
)

from .. import (
    agent_operation_states as agent_operation_states,
)
from .. import (
    job_states as job_states,
)
from ..agent_jobs import (
    AGENT_UPGRADE_RECOVERY_FENCE as AGENT_UPGRADE_RECOVERY_FENCE,
)
from ..agent_jobs import (
    AgentJobService as AgentJobService,
)
from ..agent_jobs import (
    agent_upgrade_in_flight as agent_upgrade_in_flight,
)
from ..agent_jobs import (
    schedule_agent_upgrade_retry as schedule_agent_upgrade_retry,
)
from ..agent_package_source import (
    load_package_source as load_package_source,
)
from ..agent_upgrade_contract import (
    AgentUpgradePackage as AgentUpgradePackage,
)
from ..agent_upgrade_contract import (
    AgentUpgradeRepairManifest as AgentUpgradeRepairManifest,
)
from ..agent_upgrade_contract import (
    AgentUpgradeRequestIntent as AgentUpgradeRequestIntent,
)
from ..agent_upgrade_contract import (
    AgentUpgradeRolloutPayload as AgentUpgradeRolloutPayload,
)
from ..agent_upgrade_contract import (
    AgentUpgradeRolloutResult as AgentUpgradeRolloutResult,
)
from ..bounded_retry import (
    bounded_attempts as bounded_attempts,
)
from ..categorized_errors import (
    InvalidType as InvalidType,
)
from ..categorized_errors import (
    InvalidValue as InvalidValue,
)
from ..categorized_errors import (
    MissingRecord as MissingRecord,
)
from ..lifecycle import (
    CancelRequested as CancelRequested,
)
from ..lifecycle import (
    Outcome as Outcome,
)
from ..lifecycle import (
    Reported as Reported,
)
from ..lifecycle.agent_operation import (
    AgentOperationAdapter as AgentOperationAdapter,
)
from ..lifecycle.agent_upgrade import (
    UNSUPPORTED_DISPATCH as UNSUPPORTED_DISPATCH,
)
from ..lifecycle.agent_upgrade import (
    AgentUpgradeAdapter as AgentUpgradeAdapter,
)
from ..lifecycle.evidence import (
    BookkeepingReason as BookkeepingReason,
)
from ..lifecycle.evidence import (
    retire_as_unknown as retire_as_unknown,
)
from ..models import (
    AgentNode as AgentNode,
)
from ..models import (
    AgentOperation as AgentOperation,
)
from ..models import (
    AgentOperationAttempt as AgentOperationAttempt,
)
from ..models import (
    Job as Job,
)
from ..models import (
    JobAttempt as JobAttempt,
)
from ..strict_json import (
    read_stored_model as read_stored_model,
)
from .constants import _ACTIVE_ROLLOUT_STATES as _ACTIVE_ROLLOUT_STATES
from .constants import _AGENT_UPGRADE_RECOVERY_FENCE as _AGENT_UPGRADE_RECOVERY_FENCE
from .constants import _ALREADY_CURRENT as _ALREADY_CURRENT
from .constants import _ONLINE_WINDOW as _ONLINE_WINDOW
from .constants import _SHA256 as _SHA256
from .errors import _NODE_ID as _NODE_ID
from .errors import _RELEASE_PAUSES as _RELEASE_PAUSES
from .errors import AgentUpgradeConflict as AgentUpgradeConflict
from .errors import AgentUpgradeInvalid as AgentUpgradeInvalid
from .errors import AgentUpgradeRefused as AgentUpgradeRefused
from .errors import AgentUpgradeRetryLater as AgentUpgradeRetryLater
from .errors import AgentUpgradeUnavailable as AgentUpgradeUnavailable
from .errors import _conflict_detail as _conflict_detail
from .helpers import _aware as _aware
from .helpers import _failure_detail as _failure_detail
from .helpers import _summary as _summary
from .intent import _plan_digest as _plan_digest
from .intent import _request_intent as _request_intent
from .intent import _rollout_document as _rollout_document
from .plan import AgentUpgradePlan as AgentUpgradePlan
from .service import AgentUpgradeService as AgentUpgradeService
from .stored import _stored_field as _stored_field
from .stored import _stored_node_order as _stored_node_order
from .stored import _stored_package as _stored_package
from .stored import _stored_result as _stored_result
from .stored import _stored_source as _stored_source
