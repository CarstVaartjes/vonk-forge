from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.agent_result_state import AgentResultState
from ..models.agent_result_state import check_agent_result_state
from ..models.blocker_category import BlockerCategory
from ..models.blocker_category import check_blocker_category
from ..models.error_category import check_error_category
from ..models.error_category import ErrorCategory
from ..models.failure_code import check_failure_code
from ..models.failure_code import FailureCode
from ..models.invalid_request_reason import check_invalid_request_reason
from ..models.invalid_request_reason import InvalidRequestReason
from ..models.lifecycle_effect import check_lifecycle_effect
from ..models.lifecycle_effect import LifecycleEffect
from ..models.lifecycle_event_kind import check_lifecycle_event_kind
from ..models.lifecycle_event_kind import LifecycleEventKind
from ..models.lifecycle_state import check_lifecycle_state
from ..models.lifecycle_state import LifecycleState
from ..models.lifecycle_subject import check_lifecycle_subject
from ..models.lifecycle_subject import LifecycleSubject
from ..models.migration_step import check_migration_step
from ..models.migration_step import MigrationStep
from ..models.operator_action_name import check_operator_action_name
from ..models.operator_action_name import OperatorActionName
from ..models.operator_surface import check_operator_surface
from ..models.operator_surface import OperatorSurface
from ..models.outcome_kind import check_outcome_kind
from ..models.outcome_kind import OutcomeKind
from ..models.security_refusal_reason import check_security_refusal_reason
from ..models.security_refusal_reason import SecurityRefusalReason
from ..models.state_alias import check_state_alias
from ..models.state_alias import StateAlias
from ..models.state_write_kind import check_state_write_kind
from ..models.state_write_kind import StateWriteKind
from ..models.stop_outcome import check_stop_outcome
from ..models.stop_outcome import StopOutcome
from ..models.wait_reason import check_wait_reason
from ..models.wait_reason import WaitReason
from ..models.wait_verdict import check_wait_verdict
from ..models.wait_verdict import WaitVerdict
from typing import cast






T = TypeVar("T", bound="LifecycleVocabulary")



@_attrs_define
class LifecycleVocabulary:
    """ Carrier that publishes every vocabulary enum into the wire schema.

    The model is never sent: it exists so the schema exporter, the Rust
    generator and the OpenAPI/TypeScript generators emit each closed word set
    from this one module.

        Attributes:
            agent_result_state (AgentResultState): The state words of an agent result on the wire.

                The wire keeps four words, shared with agents already deployed.  A typed
                outcome decides which one is truthful (``done`` is ``succeeded``, a
                confirmed cancellation is ``cancelled``, any other definite failure is
                ``failed`` and ``unknown`` is the legacy ``waiting-for-operator``); the
                Controller maps them onto its stored state values unchanged.
            blocker_category (BlockerCategory): The categories of the blocker allowlist (fail-closed raises).
            effect (LifecycleEffect): What is known about the real-world effect of the work.
            error_category (ErrorCategory): The only three things a lifecycle adapter may raise or report.

                ``security-refusal`` and ``invalid-request`` are decided at submit time and
                fail closed.  Everything else is ``unknown``: it is observed and reconciled,
                never parked.  The blocker allowlist's ``already-retried`` and
                ``bookkeeping-debt`` families are both ``unknown`` (see
                :func:`error_category_of`).
            event_kind (LifecycleEventKind): The kinds of event the pure transition function accepts.
            failure_code (FailureCode): Closed codes of a definite failed outcome reported by the agent.
            invalid_request_reason (InvalidRequestReason): Closed reason codes of an invalid request (submit-time input
                validation).
            lifecycle_subject (LifecycleSubject): The persisted models whose ``state`` the lifecycle core owns.
            migration_step (MigrationStep): The migration steps of the blocker audit (section 5.6) that retire a writer.
            operator_action (OperatorActionName): The operator actions a row can advertise and the core accepts.
            operator_surface (OperatorSurface): The real surfaces behind an advertised action in the blocker allowlist.
            outcome_kind (OutcomeKind): What an executor reported, as the lifecycle core sees it.

                ``done``, ``cancelled`` and a ``failed`` that is not retryable are
                *definite*: the executor says what happened, and the row ends there.
                ``unknown`` (and a retryable failure) says the effect may or may not have
                happened, which the core resolves by observing it.  On the agent wire a
                cancellation is a definite ``failed`` outcome with the
                ``operation_cancelled`` code.
            security_refusal_reason (SecurityRefusalReason): Closed reason codes of a security refusal: a real security
                boundary.

                Authentication and authorization, identity and certificate expiry, node
                revocation, enrollment, signed package metadata, host-helper authority,
                tombstone fencing, credential denial and the digest-bound destructive-effect
                checks of Run/Switch.  ``failure_classification`` derives its code set from
                this enum, so the Controller and the contract cannot disagree.
            state (LifecycleState): The nine lifecycle states: the one vocabulary a stored ``state`` speaks.

                ``superseded`` is a definite, non-failed end: a newer request replaced the
                work, so nothing is left for anyone to do.  The legacy spellings
                (``waiting``, ``partial``, ``cancelling``, ``expired``,
                ``waiting-for-operator``) are *aliases*: see :data:`STATE_ALIASES`, the one
                table that says what each of them means, per subject.
            state_alias (StateAlias): The retired spellings of a stored lifecycle state.

                They are accepted as input for one release (API filters, CLI arguments) and
                adopted when an old row is read; nothing writes them any more.  This is the
                only place the words may be spelled: the vocabulary ratchet allows them
                nowhere else.
            state_write_kind (StateWriteKind): The shapes of a lifecycle state write the writers ratchet recognises.
            stop_outcome (StopOutcome): Whether an idempotent stop confirmed that the effect is gone.
            wait_reason (WaitReason): Typed reason codes for an effect that cannot be confirmed (the *unknown* kind).

                Each code names the one fact the executor could not establish.  The free
                text of a report is for people; the Controller decides on this code.
            wait_verdict (WaitVerdict): The verdicts of the blocker allowlist for an operator wait.
     """

    agent_result_state: AgentResultState
    blocker_category: BlockerCategory
    effect: LifecycleEffect
    error_category: ErrorCategory
    event_kind: LifecycleEventKind
    failure_code: FailureCode
    invalid_request_reason: InvalidRequestReason
    lifecycle_subject: LifecycleSubject
    migration_step: MigrationStep
    operator_action: OperatorActionName
    operator_surface: OperatorSurface
    outcome_kind: OutcomeKind
    security_refusal_reason: SecurityRefusalReason
    state: LifecycleState
    state_alias: StateAlias
    state_write_kind: StateWriteKind
    stop_outcome: StopOutcome
    wait_reason: WaitReason
    wait_verdict: WaitVerdict





    def to_dict(self) -> dict[str, Any]:
        agent_result_state: str = self.agent_result_state

        blocker_category: str = self.blocker_category

        effect: str = self.effect

        error_category: str = self.error_category

        event_kind: str = self.event_kind

        failure_code: str = self.failure_code

        invalid_request_reason: str = self.invalid_request_reason

        lifecycle_subject: str = self.lifecycle_subject

        migration_step: str = self.migration_step

        operator_action: str = self.operator_action

        operator_surface: str = self.operator_surface

        outcome_kind: str = self.outcome_kind

        security_refusal_reason: str = self.security_refusal_reason

        state: str = self.state

        state_alias: str = self.state_alias

        state_write_kind: str = self.state_write_kind

        stop_outcome: str = self.stop_outcome

        wait_reason: str = self.wait_reason

        wait_verdict: str = self.wait_verdict


        field_dict: dict[str, Any] = {}

        field_dict.update({
            "agent_result_state": agent_result_state,
            "blocker_category": blocker_category,
            "effect": effect,
            "error_category": error_category,
            "event_kind": event_kind,
            "failure_code": failure_code,
            "invalid_request_reason": invalid_request_reason,
            "lifecycle_subject": lifecycle_subject,
            "migration_step": migration_step,
            "operator_action": operator_action,
            "operator_surface": operator_surface,
            "outcome_kind": outcome_kind,
            "security_refusal_reason": security_refusal_reason,
            "state": state,
            "state_alias": state_alias,
            "state_write_kind": state_write_kind,
            "stop_outcome": stop_outcome,
            "wait_reason": wait_reason,
            "wait_verdict": wait_verdict,
        })

        return field_dict



    @classmethod
    def from_dict(cls: type[T], src_dict: Mapping[str, Any]) -> T:
        d = dict(src_dict)
        agent_result_state = check_agent_result_state(d.pop("agent_result_state"))




        blocker_category = check_blocker_category(d.pop("blocker_category"))




        effect = check_lifecycle_effect(d.pop("effect"))




        error_category = check_error_category(d.pop("error_category"))




        event_kind = check_lifecycle_event_kind(d.pop("event_kind"))




        failure_code = check_failure_code(d.pop("failure_code"))




        invalid_request_reason = check_invalid_request_reason(d.pop("invalid_request_reason"))




        lifecycle_subject = check_lifecycle_subject(d.pop("lifecycle_subject"))




        migration_step = check_migration_step(d.pop("migration_step"))




        operator_action = check_operator_action_name(d.pop("operator_action"))




        operator_surface = check_operator_surface(d.pop("operator_surface"))




        outcome_kind = check_outcome_kind(d.pop("outcome_kind"))




        security_refusal_reason = check_security_refusal_reason(d.pop("security_refusal_reason"))




        state = check_lifecycle_state(d.pop("state"))




        state_alias = check_state_alias(d.pop("state_alias"))




        state_write_kind = check_state_write_kind(d.pop("state_write_kind"))




        stop_outcome = check_stop_outcome(d.pop("stop_outcome"))




        wait_reason = check_wait_reason(d.pop("wait_reason"))




        wait_verdict = check_wait_verdict(d.pop("wait_verdict"))




        lifecycle_vocabulary = cls(
            agent_result_state=agent_result_state,
            blocker_category=blocker_category,
            effect=effect,
            error_category=error_category,
            event_kind=event_kind,
            failure_code=failure_code,
            invalid_request_reason=invalid_request_reason,
            lifecycle_subject=lifecycle_subject,
            migration_step=migration_step,
            operator_action=operator_action,
            operator_surface=operator_surface,
            outcome_kind=outcome_kind,
            security_refusal_reason=security_refusal_reason,
            state=state,
            state_alias=state_alias,
            state_write_kind=state_write_kind,
            stop_outcome=stop_outcome,
            wait_reason=wait_reason,
            wait_verdict=wait_verdict,
        )

        return lifecycle_vocabulary
