"""Singular operator API for Fleet, Model and Recipe projections.

Public facade; patch dependencies in their implementation modules.
"""

import logging as logging
import traceback as traceback
from collections.abc import Mapping as Mapping
from collections.abc import Sequence as Sequence
from dataclasses import asdict as asdict
from datetime import UTC as UTC
from datetime import datetime as datetime
from datetime import timedelta as timedelta
from typing import Annotated as Annotated
from typing import Any as Any
from typing import Literal as Literal
from typing import Protocol as Protocol

from fastapi import FastAPI as FastAPI
from fastapi import HTTPException as HTTPException
from fastapi import Path as Path
from fastapi import Query as Query
from fastapi import status as status
from pydantic import ConfigDict as ConfigDict
from pydantic import Field as Field
from pydantic import model_serializer as model_serializer
from sqlalchemy import select as select
from sqlalchemy.exc import SQLAlchemyError as SQLAlchemyError
from sqlalchemy.orm import Session as Session
from sqlalchemy.orm import sessionmaker as sessionmaker
from starlette.responses import Response as Response
from vonk_agent_protocol import ControllerErrorCode as ControllerErrorCode
from vonk_agent_protocol import EnrollmentGrantState as EnrollmentGrantState
from vonk_agent_protocol import LifecycleState as LifecycleState
from vonk_agent_protocol import ModelCacheOperatorStatus as ModelCacheOperatorStatus
from vonk_agent_protocol import ObservationCause as ObservationCause
from vonk_agent_protocol import SecurityRefusalReason as SecurityRefusalReason

from .. import agent_operation_states as agent_operation_states
from ..agent_api import AgentApiServices as AgentApiServices
from ..agent_api import EnrollmentGrantResponse as EnrollmentGrantResponse
from ..agent_upgrade_contract import AgentUpgradePackage as AgentUpgradePackage
from ..agent_upgrade_contract import (
    AgentUpgradeRequestIntent as AgentUpgradeRequestIntent,
)
from ..agent_upgrades import AgentUpgradeConflict as AgentUpgradeConflict
from ..agent_upgrades import AgentUpgradeService as AgentUpgradeService
from ..auth import MUTATION_ROLES as MUTATION_ROLES
from ..auth import Actor as Actor
from ..auth import CursorError as CursorError
from ..bounded_json import BoundedJSONError as BoundedJSONError
from ..enrollment import (
    MAX_ENROLLMENT_GRANT_TTL_SECONDS as MAX_ENROLLMENT_GRANT_TTL_SECONDS,
)
from ..enrollment import EnrollmentDenied as EnrollmentDenied
from ..enrollment import RemoteRevocationUncertain as RemoteRevocationUncertain
from ..enrollment_bootstrap import accepted_installer_url as accepted_installer_url
from ..enrollment_contract import ENROLLMENT_ID_PATTERN as ENROLLMENT_ID_PATTERN
from ..enrollment_contract import EnrollmentGrant as EnrollmentGrant
from ..enrollment_contract import EnrollmentGrantStatus as EnrollmentGrantStatus
from ..enrollment_contract import EnrollmentId as EnrollmentId
from ..enrollment_contract import (
    EnrollmentObservationOutcome as EnrollmentObservationOutcome,
)
from ..enrollment_contract import (
    EnrollmentRevocationStatus as EnrollmentRevocationStatus,
)
from ..failure_evidence import AttemptPhase as AttemptPhase
from ..failure_evidence import FailedAttempt as FailedAttempt
from ..failure_evidence import FailureEvidenceBundle as FailureEvidenceBundle
from ..failure_evidence import collect_failure as collect_failure
from ..failure_evidence import failed_attempt_condition as failed_attempt_condition
from ..fleet_projection import FleetNode as FleetNode
from ..fleet_projection import FleetNodeIdentity as FleetNodeIdentity
from ..fleet_projection import FleetSnapshot as FleetSnapshot
from ..library_projection import LibrarySelectorAmbiguous as LibrarySelectorAmbiguous
from ..logging import current_request_id as current_request_id
from ..logging import log_event as log_event
from ..logging import redact_text as redact_text
from ..models import AgentOperation as AgentOperation
from ..models import AgentOperationAttempt as AgentOperationAttempt
from ..observation_transfer import (
    ObservationTransferRecord as ObservationTransferRecord,
)
from ..observation_transfer import (
    ObservationTransferResponse as ObservationTransferResponse,
)
from ..observation_transfer import observation_openapi as observation_openapi
from ..observation_transfer import observation_response as observation_response
from ..operation_api import bounded_error_responses as bounded_error_responses
from ..operation_item_contract import OperationResultFacts as OperationResultFacts
from ..platform_observation_errors import (
    ObservationCaptureUnavailable as ObservationCaptureUnavailable,
)
from ..platform_observation_errors import (
    observation_capture_unavailable_response as observation_capture_unavailable_response,
)
from ..request_fault import RequestFault as RequestFault
from ..strict_json import StrictJSONModel as StrictJSONModel
from ..strict_json import stored_document_detail as stored_document_detail
from .contracts import _NODE_PATTERN as _NODE_PATTERN
from .contracts import _SELECTOR_PATTERN as _SELECTOR_PATTERN
from .contracts import FLEET_OPERATION_IDS as FLEET_OPERATION_IDS
from .contracts import FleetActionResponse as FleetActionResponse
from .contracts import FleetEnrollRequest as FleetEnrollRequest
from .contracts import FleetLockHolder as FleetLockHolder
from .contracts import FleetLocksResponse as FleetLocksResponse
from .contracts import FleetLogEntry as FleetLogEntry
from .contracts import FleetLogResponse as FleetLogResponse
from .contracts import FleetOpenTransaction as FleetOpenTransaction
from .contracts import FleetReenrollRequest as FleetReenrollRequest
from .contracts import FleetRenameRequest as FleetRenameRequest
from .contracts import FleetUpgradeRequest as FleetUpgradeRequest
from .contracts import LogLevel as LogLevel
from .contracts import LogSource as LogSource
from .contracts import _fleet_work_state as _fleet_work_state
from .errors import _LOGGER as _LOGGER
from .errors import _domain_refusal_detail as _domain_refusal_detail
from .errors import _node as _node
from .errors import _operator_error as _operator_error
from .errors import _require_mutation as _require_mutation
from .logs import _AGENT_LOG_ENTRY_LIMIT as _AGENT_LOG_ENTRY_LIMIT
from .logs import _AGENT_LOG_LOOKBACK as _AGENT_LOG_LOOKBACK
from .logs import _AGENT_LOG_SCAN_LIMIT as _AGENT_LOG_SCAN_LIMIT
from .logs import _ATTEMPT_OUTCOME as _ATTEMPT_OUTCOME
from .logs import AgentFailureLogProvider as AgentFailureLogProvider
from .logs import _agent_log_source as _agent_log_source
from .logs import _attempt_is_parked as _attempt_is_parked
from .logs import _aware as _aware
from .logs import _failure_log_entries as _failure_log_entries
from .logs import _lease_clock as _lease_clock
from .logs import _phase_start_deadline as _phase_start_deadline
from .routes import (
    install_operator_projection_routes as install_operator_projection_routes,
)
from .services import FleetEnrollmentProvider as FleetEnrollmentProvider
from .services import FleetLogProvider as FleetLogProvider
from .services import FleetOperatorServices as FleetOperatorServices
from .services import FleetUpgradeProvider as FleetUpgradeProvider
from .services import _AgentEnrollmentAdapter as _AgentEnrollmentAdapter
from .wiring import build_fleet_operator_services as build_fleet_operator_services

__all__ = [
    "FleetActionResponse",
    "FleetEnrollRequest",
    "FleetEnrollmentProvider",
    "FleetLogEntry",
    "FleetLogProvider",
    "FleetLogResponse",
    "FleetOperatorServices",
    "FleetRenameRequest",
    "FleetUpgradeProvider",
    "FleetUpgradeRequest",
    "build_fleet_operator_services",
    "install_operator_projection_routes",
]
