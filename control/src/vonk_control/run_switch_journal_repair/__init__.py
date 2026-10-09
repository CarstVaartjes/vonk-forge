"""Public exports for run switch journal repair."""

import hashlib as hashlib
import json as json
import uuid as uuid
from dataclasses import (
    dataclass as dataclass,
)
from datetime import (
    datetime as datetime,
)
from datetime import (
    timedelta as timedelta,
)

from sqlalchemy import (
    select as select,
)
from sqlalchemy.exc import (
    DBAPIError as DBAPIError,
)
from sqlalchemy.orm import (
    Session as Session,
)
from sqlalchemy.orm import (
    sessionmaker as sessionmaker,
)
from vonk_agent_protocol import (
    LifecycleState as LifecycleState,
)
from vonk_agent_protocol import (
    LifecycleSubject as LifecycleSubject,
)
from vonk_agent_protocol import (
    ReservationState as ReservationState,
)
from vonk_agent_protocol import (
    UnknownOutcomeError as UnknownOutcomeError,
)
from vonk_agent_protocol import (
    canonical_message as canonical_message,
)
from vonk_agent_protocol import (
    is_state as is_state,
)

from .. import (
    agent_operation_states as agent_operation_states,
)
from ..admission_locking import (
    AdmissionLockBusy as AdmissionLockBusy,
)
from ..admission_locking import (
    AdmissionRowLock as AdmissionRowLock,
)
from ..admission_locking import (
    lock_admission_rows as lock_admission_rows,
)
from ..content_identity import (
    same_image as same_image,
)
from ..job_documents import (
    RecipeInstallParent as RecipeInstallParent,
)
from ..models import (
    AgentCertificate as AgentCertificate,
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
    FleetProfileApplication as FleetProfileApplication,
)
from ..models import (
    Job as Job,
)
from ..models import (
    RunSwitchJournalRepair as RunSwitchJournalRepair,
)
from ..models import (
    RunSwitchJournalRepairPending as RunSwitchJournalRepairPending,
)
from ..operation_progress import (
    stored_progress as stored_progress,
)
from ..recipe_operations import (
    current_recipe_progress_attribution as current_recipe_progress_attribution,
)
from ..run_switch_contract import (
    RunSwitchCancellation as RunSwitchCancellation,
)
from ..run_switch_contract import (
    RunSwitchOperationResult as RunSwitchOperationResult,
)
from ..run_switch_contract import (
    RunSwitchRuntimeImageResult as RunSwitchRuntimeImageResult,
)
from ..run_switch_contract import (
    RunSwitchRuntimeInstallResult as RunSwitchRuntimeInstallResult,
)
from ..run_switch_contract import (
    RunSwitchRuntimePlanResult as RunSwitchRuntimePlanResult,
)
from ..run_switch_contract import (
    RunSwitchTargetTransferEvidenceResult as RunSwitchTargetTransferEvidenceResult,
)
from ..run_switch_contract import (
    RunSwitchVerifyResult as RunSwitchVerifyResult,
)
from ..run_switch_journal_contract import (
    JournalRepairCode as JournalRepairCode,
)
from ..run_switch_journal_contract import (
    JournalRepairDisposition as JournalRepairDisposition,
)
from ..run_switch_journal_contract import (
    JournalRepairPurpose as JournalRepairPurpose,
)
from ..run_switch_journal_contract import (
    NativeProgressWitness as NativeProgressWitness,
)
from ..run_switch_journal_contract import (
    RunSwitchJournalRepairEndEvidence as RunSwitchJournalRepairEndEvidence,
)
from ..run_switch_journal_contract import (
    RunSwitchJournalRepairEvidence as RunSwitchJournalRepairEvidence,
)
from ..run_switch_journal_contract import (
    RunSwitchJournalRepairPendingState as RunSwitchJournalRepairPendingState,
)
from ..stored_json import (
    read_row_column as read_row_column,
)
from .discovery import REPAIR_WAIT as REPAIR_WAIT
from .discovery import _candidate as _candidate
from .discovery import _discover as _discover
from .discovery import _Discovery as _Discovery
from .discovery import _fingerprint as _fingerprint
from .discovery import _lock_discovery as _lock_discovery
from .discovery import _plan_digest as _plan_digest
from .discovery import is_zero_transfer_journal_fault as is_zero_transfer_journal_fault
from .discovery import journal_document as journal_document
from .ending import _end_unproven_journal as _end_unproven_journal
from .observation import (
    _observe_zero_transfer_journal as _observe_zero_transfer_journal,
)
from .observation import (
    try_repair_zero_transfer_journal as try_repair_zero_transfer_journal,
)
from .pending import REPAIR_BUDGET as REPAIR_BUDGET
from .pending import _pending as _pending
from .pending import record_repair_cancellation as record_repair_cancellation
from .proof import _prove as _prove
from .repair import _try_repair_once as _try_repair_once
