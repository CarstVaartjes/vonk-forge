from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypeVar, BinaryIO, TextIO, TYPE_CHECKING, Generator

from attrs import define as _attrs_define
from attrs import field as _attrs_field

from ..types import UNSET, Unset

from ..models.agent_result_state import AgentResultState
from ..models.agent_result_state import check_agent_result_state
from ..models.artifact_preparation import ArtifactPreparation
from ..models.artifact_preparation import check_artifact_preparation
from ..models.asset_availability import AssetAvailability
from ..models.asset_availability import check_asset_availability
from ..models.blocker_category import BlockerCategory
from ..models.blocker_category import check_blocker_category
from ..models.catalog_sync_state import CatalogSyncState
from ..models.catalog_sync_state import check_catalog_sync_state
from ..models.certificate_state import CertificateState
from ..models.certificate_state import check_certificate_state
from ..models.desired_assignment_state import check_desired_assignment_state
from ..models.desired_assignment_state import DesiredAssignmentState
from ..models.distribution_assignment_state import check_distribution_assignment_state
from ..models.distribution_assignment_state import DistributionAssignmentState
from ..models.endpoint_state import check_endpoint_state
from ..models.endpoint_state import EndpointState
from ..models.enrollment_grant_state import check_enrollment_grant_state
from ..models.enrollment_grant_state import EnrollmentGrantState
from ..models.error_category import check_error_category
from ..models.error_category import ErrorCategory
from ..models.failure_code import check_failure_code
from ..models.failure_code import FailureCode
from ..models.failure_stage import check_failure_stage
from ..models.failure_stage import FailureStage
from ..models.gateway_route_state import check_gateway_route_state
from ..models.gateway_route_state import GatewayRouteState
from ..models.host_helper_response_status import check_host_helper_response_status
from ..models.host_helper_response_status import HostHelperResponseStatus
from ..models.installation_node_state import check_installation_node_state
from ..models.installation_node_state import InstallationNodeState
from ..models.installation_state import check_installation_state
from ..models.installation_state import InstallationState
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
from ..models.model_cache_operator_status import check_model_cache_operator_status
from ..models.model_cache_operator_status import ModelCacheOperatorStatus
from ..models.model_file_state import check_model_file_state
from ..models.model_file_state import ModelFileState
from ..models.observation_cause import check_observation_cause
from ..models.observation_cause import ObservationCause
from ..models.observed_assignment_state import check_observed_assignment_state
from ..models.observed_assignment_state import ObservedAssignmentState
from ..models.operator_action_name import check_operator_action_name
from ..models.operator_action_name import OperatorActionName
from ..models.operator_surface import check_operator_surface
from ..models.operator_surface import OperatorSurface
from ..models.outcome_kind import check_outcome_kind
from ..models.outcome_kind import OutcomeKind
from ..models.placement_install_state import check_placement_install_state
from ..models.placement_install_state import PlacementInstallState
from ..models.placement_load_state import check_placement_load_state
from ..models.placement_load_state import PlacementLoadState
from ..models.profile_action import check_profile_action
from ..models.profile_action import ProfileAction
from ..models.profile_cancellation_cause import check_profile_cancellation_cause
from ..models.profile_cancellation_cause import ProfileCancellationCause
from ..models.profile_child_job_kind import check_profile_child_job_kind
from ..models.profile_child_job_kind import ProfileChildJobKind
from ..models.profile_child_phase import check_profile_child_phase
from ..models.profile_child_phase import ProfileChildPhase
from ..models.profile_child_source import check_profile_child_source
from ..models.profile_child_source import ProfileChildSource
from ..models.profile_document_state import check_profile_document_state
from ..models.profile_document_state import ProfileDocumentState
from ..models.profile_effect_state import check_profile_effect_state
from ..models.profile_effect_state import ProfileEffectState
from ..models.profile_installation_policy import check_profile_installation_policy
from ..models.profile_installation_policy import ProfileInstallationPolicy
from ..models.profile_operation_kind import check_profile_operation_kind
from ..models.profile_operation_kind import ProfileOperationKind
from ..models.profile_projection_kind import check_profile_projection_kind
from ..models.profile_projection_kind import ProfileProjectionKind
from ..models.profile_reason_severity import check_profile_reason_severity
from ..models.profile_reason_severity import ProfileReasonSeverity
from ..models.profile_reported_phase import check_profile_reported_phase
from ..models.profile_reported_phase import ProfileReportedPhase
from ..models.profile_retry_disposition import check_profile_retry_disposition
from ..models.profile_retry_disposition import ProfileRetryDisposition
from ..models.profile_switch_child_kind import check_profile_switch_child_kind
from ..models.profile_switch_child_kind import ProfileSwitchChildKind
from ..models.progress_phase import check_progress_phase
from ..models.progress_phase import ProgressPhase
from ..models.reservation_state import check_reservation_state
from ..models.reservation_state import ReservationState
from ..models.resource_blocker_code import check_resource_blocker_code
from ..models.resource_blocker_code import ResourceBlockerCode
from ..models.route_publication_state import check_route_publication_state
from ..models.route_publication_state import RoutePublicationState
from ..models.route_state import check_route_state
from ..models.route_state import RouteState
from ..models.run_admission_code import check_run_admission_code
from ..models.run_admission_code import RunAdmissionCode
from ..models.run_state import check_run_state
from ..models.run_state import RunState
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

                Unknown effects are observed by the Controller with bounded retries. The
                retired operator-wait spelling is adopted only when reading old receipts.
            artifact_preparation (ArtifactPreparation): The stages of an artifact job before it is submitted.

                These are preparation, not execution: a job is ``draft`` while its inputs are
                uploaded and ``ready`` once they are complete.  Its lifecycle ``state`` begins
                at ``queued`` on submit and is absent until then.  The old spelling kept both
                in the one ``state`` word, which is why :func:`legacy_preparation` exists.
            asset_availability (AssetAvailability): What is known about a model asset on a Spark's disk.
            blocker_category (BlockerCategory): The categories of the blocker allowlist (fail-closed raises).
            catalog_sync_state (CatalogSyncState): The outcome of a catalog synchronization, as the catalog shows it.
            certificate_state (CertificateState): The standing of a node's client certificate, as the fleet projection shows
                it.
            desired_assignment_state (DesiredAssignmentState): What a fleet-profile assignment is asked to become on its
                Sparks.
            distribution_assignment_state (DistributionAssignmentState): Whether a node may still fetch the artifacts of a
                distribution assignment.
            effect (LifecycleEffect): What is known about the real-world effect of the work.
            endpoint_state (EndpointState): Whether the endpoint of a fleet-profile assignment can be reached.
            enrollment_grant_state (EnrollmentGrantState): The standing of an enrollment grant.
            error_category (ErrorCategory): The only three things a lifecycle adapter may raise or report.

                ``security-refusal`` and ``invalid-request`` are decided at submit time and
                fail closed.  Everything else is ``unknown``: it is observed and reconciled,
                never parked.  The blocker allowlist's ``already-retried`` and
                ``bookkeeping-debt`` families are both ``unknown`` (see
                :func:`error_category_of`).
            event_kind (LifecycleEventKind): The kinds of event the pure transition function accepts.
            failure_code (FailureCode): Closed codes of a definite failed outcome reported by the agent.
            failure_stage (FailureStage): The step an operation stopped at: a short, stable, secret-free word.
            gateway_route_state (GatewayRouteState): What the inference gateway currently serves: published routes, or
                maintenance.

                ``unavailable`` is the Controller's own word for a gateway whose marker it
                cannot read; the gateway never writes it.
            host_helper_response_status (HostHelperResponseStatus): The verdict a privileged-helper reply carries.
            installation_node_state (InstallationNodeState): The condition of one rank of an installation on its Spark.
            installation_state (InstallationState): The condition of a recipe installation across its Sparks.
            invalid_request_reason (InvalidRequestReason): Closed reason codes of an invalid request (submit-time input
                validation).
            lifecycle_subject (LifecycleSubject): The persisted models whose ``state`` the lifecycle core owns.
            migration_step (MigrationStep): The migration steps of the blocker audit (section 5.6) that retire a writer.
            model_cache_operator_status (ModelCacheOperatorStatus): The operator-facing word of a model-cache operation that
                is not a stored state.

                ``accepted``: the request is recorded and not yet picked up.  Every other state
                an operator sees is a :class:`~vonk_agent_protocol.LifecycleState` (a cancel
                under way is ``observing`` with its cancellation intent).
            model_file_state (ModelFileState): The condition of one model file a node holds.
            observation_cause (ObservationCause): Why an attempt is being observed rather than settled.

                An attempt that ended without a definite answer is ``observing``; this says
                what left it unanswered.  ``reported-unknown``: the executor said it could not
                confirm the effect.  ``lease-lapsed``: the executor stopped reporting.  The old
                spellings carried this in the state word itself (``waiting-for-operator`` and
                ``expired``), which is why an adopted attempt also yields its cause.
            observed_assignment_state (ObservedAssignmentState): Where a fleet-profile assignment stands on the Sparks, as
                observed.
            operator_action (OperatorActionName): The operator actions a row can advertise and the core accepts.
            operator_surface (OperatorSurface): The real surfaces behind an advertised action in the blocker allowlist.
            outcome_kind (OutcomeKind): What an executor reported, as the lifecycle core sees it.

                ``done``, ``cancelled`` and a ``failed`` that is not retryable are
                *definite*: the executor says what happened, and the row ends there.
                ``unknown`` (and a retryable failure) says the effect may or may not have
                happened, which the core resolves by observing it.  On the agent wire a
                cancellation is a definite ``failed`` outcome with the
                ``operation_cancelled`` code.
            placement_install_state (PlacementInstallState): How much of a placement's installation is already on its
                Sparks.
            placement_load_state (PlacementLoadState): Whether a placement's recipe is loaded on its Sparks.
            profile_action (ProfileAction): Fleet profile Action contract words.
            profile_cancellation_cause (ProfileCancellationCause): Fleet profile CancellationCause contract words.
            profile_child_job_kind (ProfileChildJobKind): Fleet profile ChildJobKind contract words.
            profile_child_phase (ProfileChildPhase): Fleet profile ChildPhase contract words.
            profile_child_source (ProfileChildSource): Fleet profile ChildSource contract words.
            profile_document_state (ProfileDocumentState): Profile views and catalogue documents used by profile resolution.
            profile_effect_state (ProfileEffectState): Fleet profile EffectState contract words.
            profile_installation_policy (ProfileInstallationPolicy): Fleet profile InstallationPolicy contract words.
            profile_operation_kind (ProfileOperationKind): Fleet profile OperationKind contract words.
            profile_projection_kind (ProfileProjectionKind): Typed identities in profile effects and operation projections.
            profile_reason_severity (ProfileReasonSeverity): A profile reason and its upstream assessment severity.
            profile_reported_phase (ProfileReportedPhase): Run-switch phase projected into a profile child checkpoint.
            profile_retry_disposition (ProfileRetryDisposition): An accepted profile intent either retries or ends as
                superseded.
            profile_switch_child_kind (ProfileSwitchChildKind): Fleet profile SwitchChildKind contract words.
            progress_phase (ProgressPhase): What an operation is doing, as the measured progress names it.
            reservation_state (ReservationState): The standing of a resource reservation.
            resource_blocker_code (ResourceBlockerCode): The capacity-fit codes the resource planner gives a node that
                cannot fit.

                ``insufficient`` is the family prefix a run admission maps onto
                ``run.insufficient_memory``; the planner itself names the exact
                ``insufficient_capacity*`` member.
            route_publication_state (RoutePublicationState): The phases of one atomic route publication.
            route_state (RouteState): Whether a run's inference route is published to the gateway.
            run_admission_code (RunAdmissionCode): The typed codes a run admission names for a refusal, blocker or wait.

                ``capacity_busy`` is lock contention only.  Every other reason an admission
                must wait or is refused carries its own member, so a waiting operation shows
                the real cause.  The retryable blockers (a plan that may become admissible
                by itself) are a subset the Controller derives from these members.
            run_state (RunState): The condition of a recipe run (and of each of its ranks).
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
    artifact_preparation: ArtifactPreparation
    asset_availability: AssetAvailability
    blocker_category: BlockerCategory
    catalog_sync_state: CatalogSyncState
    certificate_state: CertificateState
    desired_assignment_state: DesiredAssignmentState
    distribution_assignment_state: DistributionAssignmentState
    effect: LifecycleEffect
    endpoint_state: EndpointState
    enrollment_grant_state: EnrollmentGrantState
    error_category: ErrorCategory
    event_kind: LifecycleEventKind
    failure_code: FailureCode
    failure_stage: FailureStage
    gateway_route_state: GatewayRouteState
    host_helper_response_status: HostHelperResponseStatus
    installation_node_state: InstallationNodeState
    installation_state: InstallationState
    invalid_request_reason: InvalidRequestReason
    lifecycle_subject: LifecycleSubject
    migration_step: MigrationStep
    model_cache_operator_status: ModelCacheOperatorStatus
    model_file_state: ModelFileState
    observation_cause: ObservationCause
    observed_assignment_state: ObservedAssignmentState
    operator_action: OperatorActionName
    operator_surface: OperatorSurface
    outcome_kind: OutcomeKind
    placement_install_state: PlacementInstallState
    placement_load_state: PlacementLoadState
    profile_action: ProfileAction
    profile_cancellation_cause: ProfileCancellationCause
    profile_child_job_kind: ProfileChildJobKind
    profile_child_phase: ProfileChildPhase
    profile_child_source: ProfileChildSource
    profile_document_state: ProfileDocumentState
    profile_effect_state: ProfileEffectState
    profile_installation_policy: ProfileInstallationPolicy
    profile_operation_kind: ProfileOperationKind
    profile_projection_kind: ProfileProjectionKind
    profile_reason_severity: ProfileReasonSeverity
    profile_reported_phase: ProfileReportedPhase
    profile_retry_disposition: ProfileRetryDisposition
    profile_switch_child_kind: ProfileSwitchChildKind
    progress_phase: ProgressPhase
    reservation_state: ReservationState
    resource_blocker_code: ResourceBlockerCode
    route_publication_state: RoutePublicationState
    route_state: RouteState
    run_admission_code: RunAdmissionCode
    run_state: RunState
    security_refusal_reason: SecurityRefusalReason
    state: LifecycleState
    state_alias: StateAlias
    state_write_kind: StateWriteKind
    stop_outcome: StopOutcome
    wait_reason: WaitReason
    wait_verdict: WaitVerdict





    def to_dict(self) -> dict[str, Any]:
        agent_result_state: str = self.agent_result_state

        artifact_preparation: str = self.artifact_preparation

        asset_availability: str = self.asset_availability

        blocker_category: str = self.blocker_category

        catalog_sync_state: str = self.catalog_sync_state

        certificate_state: str = self.certificate_state

        desired_assignment_state: str = self.desired_assignment_state

        distribution_assignment_state: str = self.distribution_assignment_state

        effect: str = self.effect

        endpoint_state: str = self.endpoint_state

        enrollment_grant_state: str = self.enrollment_grant_state

        error_category: str = self.error_category

        event_kind: str = self.event_kind

        failure_code: str = self.failure_code

        failure_stage: str = self.failure_stage

        gateway_route_state: str = self.gateway_route_state

        host_helper_response_status: str = self.host_helper_response_status

        installation_node_state: str = self.installation_node_state

        installation_state: str = self.installation_state

        invalid_request_reason: str = self.invalid_request_reason

        lifecycle_subject: str = self.lifecycle_subject

        migration_step: str = self.migration_step

        model_cache_operator_status: str = self.model_cache_operator_status

        model_file_state: str = self.model_file_state

        observation_cause: str = self.observation_cause

        observed_assignment_state: str = self.observed_assignment_state

        operator_action: str = self.operator_action

        operator_surface: str = self.operator_surface

        outcome_kind: str = self.outcome_kind

        placement_install_state: str = self.placement_install_state

        placement_load_state: str = self.placement_load_state

        profile_action: str = self.profile_action

        profile_cancellation_cause: str = self.profile_cancellation_cause

        profile_child_job_kind: str = self.profile_child_job_kind

        profile_child_phase: str = self.profile_child_phase

        profile_child_source: str = self.profile_child_source

        profile_document_state: str = self.profile_document_state

        profile_effect_state: str = self.profile_effect_state

        profile_installation_policy: str = self.profile_installation_policy

        profile_operation_kind: str = self.profile_operation_kind

        profile_projection_kind: str = self.profile_projection_kind

        profile_reason_severity: str = self.profile_reason_severity

        profile_reported_phase: str = self.profile_reported_phase

        profile_retry_disposition: str = self.profile_retry_disposition

        profile_switch_child_kind: str = self.profile_switch_child_kind

        progress_phase: str = self.progress_phase

        reservation_state: str = self.reservation_state

        resource_blocker_code: str = self.resource_blocker_code

        route_publication_state: str = self.route_publication_state

        route_state: str = self.route_state

        run_admission_code: str = self.run_admission_code

        run_state: str = self.run_state

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
            "artifact_preparation": artifact_preparation,
            "asset_availability": asset_availability,
            "blocker_category": blocker_category,
            "catalog_sync_state": catalog_sync_state,
            "certificate_state": certificate_state,
            "desired_assignment_state": desired_assignment_state,
            "distribution_assignment_state": distribution_assignment_state,
            "effect": effect,
            "endpoint_state": endpoint_state,
            "enrollment_grant_state": enrollment_grant_state,
            "error_category": error_category,
            "event_kind": event_kind,
            "failure_code": failure_code,
            "failure_stage": failure_stage,
            "gateway_route_state": gateway_route_state,
            "host_helper_response_status": host_helper_response_status,
            "installation_node_state": installation_node_state,
            "installation_state": installation_state,
            "invalid_request_reason": invalid_request_reason,
            "lifecycle_subject": lifecycle_subject,
            "migration_step": migration_step,
            "model_cache_operator_status": model_cache_operator_status,
            "model_file_state": model_file_state,
            "observation_cause": observation_cause,
            "observed_assignment_state": observed_assignment_state,
            "operator_action": operator_action,
            "operator_surface": operator_surface,
            "outcome_kind": outcome_kind,
            "placement_install_state": placement_install_state,
            "placement_load_state": placement_load_state,
            "profile_action": profile_action,
            "profile_cancellation_cause": profile_cancellation_cause,
            "profile_child_job_kind": profile_child_job_kind,
            "profile_child_phase": profile_child_phase,
            "profile_child_source": profile_child_source,
            "profile_document_state": profile_document_state,
            "profile_effect_state": profile_effect_state,
            "profile_installation_policy": profile_installation_policy,
            "profile_operation_kind": profile_operation_kind,
            "profile_projection_kind": profile_projection_kind,
            "profile_reason_severity": profile_reason_severity,
            "profile_reported_phase": profile_reported_phase,
            "profile_retry_disposition": profile_retry_disposition,
            "profile_switch_child_kind": profile_switch_child_kind,
            "progress_phase": progress_phase,
            "reservation_state": reservation_state,
            "resource_blocker_code": resource_blocker_code,
            "route_publication_state": route_publication_state,
            "route_state": route_state,
            "run_admission_code": run_admission_code,
            "run_state": run_state,
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




        artifact_preparation = check_artifact_preparation(d.pop("artifact_preparation"))




        asset_availability = check_asset_availability(d.pop("asset_availability"))




        blocker_category = check_blocker_category(d.pop("blocker_category"))




        catalog_sync_state = check_catalog_sync_state(d.pop("catalog_sync_state"))




        certificate_state = check_certificate_state(d.pop("certificate_state"))




        desired_assignment_state = check_desired_assignment_state(d.pop("desired_assignment_state"))




        distribution_assignment_state = check_distribution_assignment_state(d.pop("distribution_assignment_state"))




        effect = check_lifecycle_effect(d.pop("effect"))




        endpoint_state = check_endpoint_state(d.pop("endpoint_state"))




        enrollment_grant_state = check_enrollment_grant_state(d.pop("enrollment_grant_state"))




        error_category = check_error_category(d.pop("error_category"))




        event_kind = check_lifecycle_event_kind(d.pop("event_kind"))




        failure_code = check_failure_code(d.pop("failure_code"))




        failure_stage = check_failure_stage(d.pop("failure_stage"))




        gateway_route_state = check_gateway_route_state(d.pop("gateway_route_state"))




        host_helper_response_status = check_host_helper_response_status(d.pop("host_helper_response_status"))




        installation_node_state = check_installation_node_state(d.pop("installation_node_state"))




        installation_state = check_installation_state(d.pop("installation_state"))




        invalid_request_reason = check_invalid_request_reason(d.pop("invalid_request_reason"))




        lifecycle_subject = check_lifecycle_subject(d.pop("lifecycle_subject"))




        migration_step = check_migration_step(d.pop("migration_step"))




        model_cache_operator_status = check_model_cache_operator_status(d.pop("model_cache_operator_status"))




        model_file_state = check_model_file_state(d.pop("model_file_state"))




        observation_cause = check_observation_cause(d.pop("observation_cause"))




        observed_assignment_state = check_observed_assignment_state(d.pop("observed_assignment_state"))




        operator_action = check_operator_action_name(d.pop("operator_action"))




        operator_surface = check_operator_surface(d.pop("operator_surface"))




        outcome_kind = check_outcome_kind(d.pop("outcome_kind"))




        placement_install_state = check_placement_install_state(d.pop("placement_install_state"))




        placement_load_state = check_placement_load_state(d.pop("placement_load_state"))




        profile_action = check_profile_action(d.pop("profile_action"))




        profile_cancellation_cause = check_profile_cancellation_cause(d.pop("profile_cancellation_cause"))




        profile_child_job_kind = check_profile_child_job_kind(d.pop("profile_child_job_kind"))




        profile_child_phase = check_profile_child_phase(d.pop("profile_child_phase"))




        profile_child_source = check_profile_child_source(d.pop("profile_child_source"))




        profile_document_state = check_profile_document_state(d.pop("profile_document_state"))




        profile_effect_state = check_profile_effect_state(d.pop("profile_effect_state"))




        profile_installation_policy = check_profile_installation_policy(d.pop("profile_installation_policy"))




        profile_operation_kind = check_profile_operation_kind(d.pop("profile_operation_kind"))




        profile_projection_kind = check_profile_projection_kind(d.pop("profile_projection_kind"))




        profile_reason_severity = check_profile_reason_severity(d.pop("profile_reason_severity"))




        profile_reported_phase = check_profile_reported_phase(d.pop("profile_reported_phase"))




        profile_retry_disposition = check_profile_retry_disposition(d.pop("profile_retry_disposition"))




        profile_switch_child_kind = check_profile_switch_child_kind(d.pop("profile_switch_child_kind"))




        progress_phase = check_progress_phase(d.pop("progress_phase"))




        reservation_state = check_reservation_state(d.pop("reservation_state"))




        resource_blocker_code = check_resource_blocker_code(d.pop("resource_blocker_code"))




        route_publication_state = check_route_publication_state(d.pop("route_publication_state"))




        route_state = check_route_state(d.pop("route_state"))




        run_admission_code = check_run_admission_code(d.pop("run_admission_code"))




        run_state = check_run_state(d.pop("run_state"))




        security_refusal_reason = check_security_refusal_reason(d.pop("security_refusal_reason"))




        state = check_lifecycle_state(d.pop("state"))




        state_alias = check_state_alias(d.pop("state_alias"))




        state_write_kind = check_state_write_kind(d.pop("state_write_kind"))




        stop_outcome = check_stop_outcome(d.pop("stop_outcome"))




        wait_reason = check_wait_reason(d.pop("wait_reason"))




        wait_verdict = check_wait_verdict(d.pop("wait_verdict"))




        lifecycle_vocabulary = cls(
            agent_result_state=agent_result_state,
            artifact_preparation=artifact_preparation,
            asset_availability=asset_availability,
            blocker_category=blocker_category,
            catalog_sync_state=catalog_sync_state,
            certificate_state=certificate_state,
            desired_assignment_state=desired_assignment_state,
            distribution_assignment_state=distribution_assignment_state,
            effect=effect,
            endpoint_state=endpoint_state,
            enrollment_grant_state=enrollment_grant_state,
            error_category=error_category,
            event_kind=event_kind,
            failure_code=failure_code,
            failure_stage=failure_stage,
            gateway_route_state=gateway_route_state,
            host_helper_response_status=host_helper_response_status,
            installation_node_state=installation_node_state,
            installation_state=installation_state,
            invalid_request_reason=invalid_request_reason,
            lifecycle_subject=lifecycle_subject,
            migration_step=migration_step,
            model_cache_operator_status=model_cache_operator_status,
            model_file_state=model_file_state,
            observation_cause=observation_cause,
            observed_assignment_state=observed_assignment_state,
            operator_action=operator_action,
            operator_surface=operator_surface,
            outcome_kind=outcome_kind,
            placement_install_state=placement_install_state,
            placement_load_state=placement_load_state,
            profile_action=profile_action,
            profile_cancellation_cause=profile_cancellation_cause,
            profile_child_job_kind=profile_child_job_kind,
            profile_child_phase=profile_child_phase,
            profile_child_source=profile_child_source,
            profile_document_state=profile_document_state,
            profile_effect_state=profile_effect_state,
            profile_installation_policy=profile_installation_policy,
            profile_operation_kind=profile_operation_kind,
            profile_projection_kind=profile_projection_kind,
            profile_reason_severity=profile_reason_severity,
            profile_reported_phase=profile_reported_phase,
            profile_retry_disposition=profile_retry_disposition,
            profile_switch_child_kind=profile_switch_child_kind,
            progress_phase=progress_phase,
            reservation_state=reservation_state,
            resource_blocker_code=resource_blocker_code,
            route_publication_state=route_publication_state,
            route_state=route_state,
            run_admission_code=run_admission_code,
            run_state=run_state,
            security_refusal_reason=security_refusal_reason,
            state=state,
            state_alias=state_alias,
            state_write_kind=state_write_kind,
            stop_outcome=stop_outcome,
            wait_reason=wait_reason,
            wait_verdict=wait_verdict,
        )

        return lifecycle_vocabulary
