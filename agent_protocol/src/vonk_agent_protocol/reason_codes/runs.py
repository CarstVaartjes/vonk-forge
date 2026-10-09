"""Runs reason codes."""

from ..wire_model import WireEnum


class RunSwitchCode(WireEnum):
    """Run/Switch phase blockers, waits and failure codes."""

    ACTIVE_RUN_CONFLICT = "run-switch.active-run-conflict"
    ADVANCE_FAILED = "run-switch.advance-failed"
    AGENT_UPGRADE_REQUIRED = "run-switch.agent-upgrade-required"
    ARTIFACT_IDENTITY_UNKNOWN = "run-switch.artifact-identity-unknown"
    ARTIFACT_INSPECTION_UNAVAILABLE = "run-switch.artifact-inspection-unavailable"
    ARTIFACT_MANIFEST_UNKNOWN = "run-switch.artifact-manifest-unknown"
    ARTIFACT_PHASE_EXECUTOR_UNAVAILABLE = (
        "run-switch.artifact-phase-executor-unavailable"
    )
    ARTIFACT_VERIFICATION_RESULT_INVALID = (
        "run-switch.artifact-verification-result-invalid"
    )
    CLEANUP_RECLAIM_EVIDENCE_INVALID = "run-switch.cleanup-reclaim-evidence-invalid"
    CLEANUP_RECLAIMED_BYTES_EXCEED_PLAN = (
        "run-switch.cleanup-reclaimed-bytes-exceed-plan"
    )
    CLEANUP_REFERENCE_PROTECTION_EVIDENCE_INVALID = (
        "run-switch.cleanup-reference-protection-evidence-invalid"
    )
    CLEANUP_REFERENCE_PROTECTION_OVERLAP = (
        "run-switch.cleanup-reference-protection-overlap"
    )
    CLEANUP_SCOPE_INVALID = "run-switch.cleanup-scope-invalid"
    CONTAINER_BUILD_EVIDENCE_INVALID = "run-switch.container-build-evidence-invalid"
    CONTAINER_BUILD_EXECUTOR_UNAVAILABLE = (
        "run-switch.container-build-executor-unavailable"
    )
    CONTAINER_BUILD_IDENTITY_UNAVAILABLE = (
        "run-switch.container-build-identity-unavailable"
    )
    CONTAINER_BUILD_PARENT_CHANGED = "run-switch.container-build-parent-changed"
    CONTAINER_BUILD_PARENT_INVALID = "run-switch.container-build-parent-invalid"
    CONTAINER_BUILD_PLAN_INVALID = "run-switch.container-build-plan-invalid"
    CONTAINER_BUILD_RECEIPT_UNAVAILABLE = (
        "run-switch.container-build-receipt-unavailable"
    )
    CONTAINER_BUILD_REQUIRED = "run-switch.container-build-required"
    CONTAINER_BUILD_STATE_INVALID = "run-switch.container-build-state-invalid"
    CONTAINER_BUILD_UNAVAILABLE = "run-switch.container-build-unavailable"
    CROSS_GROUP_CONFLICT = "run-switch.cross-group_conflict"
    DISK_ENVELOPE_INVALID = "run-switch.disk-envelope-invalid"
    DISK_EVICTION_PLANNED = "run-switch.disk-eviction-planned"
    EFFECT_UNCERTAIN = "run-switch.effect-uncertain"
    FINAL_VERIFICATION = "run-switch.final-verification"
    FINAL_VERIFICATION_CLOCK_INVALID = "run-switch.final-verification-clock-invalid"
    FINAL_VERIFICATION_FAILED = "run-switch.final-verification-failed"
    FINAL_VERIFICATION_TIMEOUT = "run-switch.final-verification-timeout"
    FINAL_VERIFICATION_UNAVAILABLE = "run-switch.final-verification-unavailable"
    INSTALL_EXECUTOR_UNAVAILABLE = "run-switch.install-executor-unavailable"
    INSTALL_PREPARATION_UNAVAILABLE = "run-switch.install-preparation-unavailable"
    INSTALLATION_HANDOFF_UNAVAILABLE = "run-switch.installation-handoff-unavailable"
    INSTALLATION_IDENTITY_CHANGED = "run-switch.installation-identity-changed"
    INSTALLATION_IDENTITY_UNAVAILABLE = "run-switch.installation-identity-unavailable"
    INSTALLATION_MEMBERSHIP_CHANGED = "run-switch.installation-membership-changed"
    INSTALLATION_PREPARATION_UNAVAILABLE = (
        "run-switch.installation-preparation-unavailable"
    )
    INSTALLATION_VERIFICATION_FAILED = "run-switch.installation-verification-failed"
    INSTALLATION_VERIFICATION_UNAVAILABLE = (
        "run-switch.installation-verification-unavailable"
    )
    INSUFFICIENT_DISK = "run-switch.insufficient-disk"
    INSUFFICIENT_MEMORY = "run-switch.insufficient-memory"
    INTERFACE_INVALID = "run-switch.interface-invalid"
    INVENTORY_STALE = "run-switch.inventory-stale"
    INVENTORY_UNKNOWN = "run-switch.inventory-unknown"
    MAPPING_GROUP_MISMATCH = "run-switch.mapping_group_mismatch"
    MAPPING_INVALID = "run-switch.mapping_invalid"
    MAPPING_MATERIALIZATION_UNAVAILABLE = (
        "run-switch.mapping_materialization_unavailable"
    )
    MEMORY_ENVELOPE_INVALID = "run-switch.memory-envelope-invalid"
    MODEL_DOWNLOAD_ARTIFACT_SET_MISMATCH = (
        "run-switch.model-download-artifact-set-mismatch"
    )
    MODEL_DOWNLOAD_BYTE_EVIDENCE_MISMATCH = (
        "run-switch.model-download-byte-evidence-mismatch"
    )
    MODEL_DOWNLOAD_COVERAGE_INCOMPLETE = "run-switch.model-download-coverage-incomplete"
    MODEL_RECIPE_MISMATCH = "run-switch.model_recipe_mismatch"
    MODEL_REVISION_UNAVAILABLE = "run-switch.model_revision_unavailable"
    NAS_COVERAGE_UNKNOWN = "run-switch.nas-coverage-unknown"
    NAS_DOWNLOAD_BLOCKED = "run-switch.nas-download-blocked"
    NAS_DOWNLOAD_REQUIRED = "run-switch.nas-download-required"
    OPTION_INVALID = "run-switch.option_invalid"
    PHASE_RETRY = "run-switch.phase-retry"
    PLAN_REFRESH_UNAVAILABLE = "run-switch.plan-refresh-unavailable"
    PLAN_TARGETS_CHANGED = "run-switch.plan-targets-changed"
    POST_STOP_INVENTORY_PENDING = "run-switch.post-stop-inventory-pending"
    POST_STOP_MEMORY_POOL_CHANGED = "run-switch.post-stop-memory-pool-changed"
    PREFLIGHT_RECIPE_CHANGED = "run-switch.preflight-recipe-changed"
    PREPARE_SUBPHASE_UNSUPPORTED = "run-switch.prepare-subphase-unsupported"
    PROFILE_INCOMPLETE_MULTI_SPARK_MODEL = (
        "run-switch.profile.incomplete_multi_spark_model"
    )
    PROFILE_STOP_SCOPE_CHANGED = "run-switch.profile_stop_scope_changed"
    RECEIPT_INVALID = "run-switch.receipt_invalid"
    RECIPE_BUILD_COMPATIBILITY_UNKNOWN = "run-switch.recipe-build-compatibility-unknown"
    RECIPE_BUILD_INCOMPATIBLE = "run-switch.recipe-build-incompatible"
    RECIPE_BUILD_UNAVAILABLE = "run-switch.recipe-build-unavailable"
    RECIPE_DEPENDENCIES_UNAVAILABLE = "run-switch.recipe_dependencies_unavailable"
    RECIPE_DIGEST_CHANGED = "run-switch.recipe_digest_changed"
    RECIPE_UNRESOLVED = "run-switch.recipe_unresolved"
    RECONCILIATION_ASSESSMENT_UNAVAILABLE = (
        "run-switch.reconciliation-assessment-unavailable"
    )
    RECONCILIATION_AUTHORITY_UNAVAILABLE = (
        "run-switch.reconciliation-authority-unavailable"
    )
    RECONCILIATION_PREREQUISITE = "run-switch.reconciliation-prerequisite"
    RECONCILIATION_RECEIPTS_RETAINED = "run-switch.reconciliation-receipts-retained"
    RECONCILIATION_STATE_VERIFICATION_FAILED = (
        "run-switch.reconciliation-state-verification-failed"
    )
    RECONCILIATION_VERIFICATION_FAILED = "run-switch.reconciliation-verification-failed"
    REQUEST_KEY_REUSED_DIFFERENTLY = "run-switch.request_key_reused_differently"
    RESOURCE_CONTRACT_INVALID = "run-switch.resource-contract-invalid"
    RESOURCE_INSUFFICIENT = "run-switch.resource.insufficient"
    RESOURCE_INSUFFICIENT_CAPACITY = "run-switch.resource.insufficient_capacity"
    RESOURCE_INSUFFICIENT_CAPACITY_AFTER_STOP = (
        "run-switch.resource.insufficient_capacity_after_stop"
    )
    RESOURCE_INSUFFICIENT_RESERVATION_BUDGET = (
        "run-switch.resource.insufficient_reservation_budget"
    )
    RESOURCE_RESIDENT_USAGE_UNKNOWN = "run-switch.resource.resident_usage_unknown"
    RUN_NOT_ACTIVE = "run-switch.run-not-active"
    RUN_ADMISSION_BLOCKED = "run-switch.run_admission_blocked"
    RUN_ADMISSION_UNAVAILABLE = "run-switch.run_admission_unavailable"
    RUNTIME_BUILD_VERIFICATION_MISMATCH = (
        "run-switch.runtime-build-verification-mismatch"
    )
    RUNTIME_IMAGE_AUTHORIZATION_MISMATCH = (
        "run-switch.runtime-image-authorization-mismatch"
    )
    RUNTIME_IMAGE_EXECUTOR_UNAVAILABLE = "run-switch.runtime-image-executor-unavailable"
    RUNTIME_IMAGE_OWNER_CHANGED = "run-switch.runtime-image-owner-changed"
    RUNTIME_IMAGE_PREPARATION_LAYOUT_MISMATCH = (
        "run-switch.runtime-image-preparation-layout-mismatch"
    )
    RUNTIME_IMAGE_PREPARATION_RECEIPT_INVALID = (
        "run-switch.runtime-image-preparation-receipt-invalid"
    )
    RUNTIME_IMAGE_PREPARING = "run-switch.runtime-image-preparing"
    RUNTIME_IMAGE_REFERENCE_IDENTITY_MISMATCH = (
        "run-switch.runtime-image-reference-identity-mismatch"
    )
    RUNTIME_IMAGE_WAITING_WITHOUT_CHILD = (
        "run-switch.runtime-image-waiting-without-child"
    )
    SPARK_UNAVAILABLE = "run-switch.spark-unavailable"
    START_OBSERVATION = "run-switch.start-observation"
    START_OBSERVATION_EXPIRED = "run-switch.start-observation-expired"
    START_INSTALLATION_UNAVAILABLE = "run-switch.start_installation_unavailable"
    STOP_PLAN_UNAVAILABLE = "run-switch.stop-plan-unavailable"
    STOP_STILL_UNRESOLVED_AFTER_CANCELLATION = (
        "run-switch.stop-still-unresolved-after-cancellation"
    )
    STOP_TARGET_DISAPPEARED = "run-switch.stop-target-disappeared"
    STOPPED_RUN_IDENTITY_CHANGED = "run-switch.stopped-run-identity-changed"
    STOPPED_RUN_MEMBERSHIP_CHANGED = "run-switch.stopped-run-membership-changed"
    TARGET_NOT_ACTIVE = "run-switch.target-not-active"
    TRANSFER_BYTE_EVIDENCE_INVALID = "run-switch.transfer-byte-evidence-invalid"
    UNINSTALL_ASSESSMENT_UNAVAILABLE = "run-switch.uninstall-assessment-unavailable"
    UNINSTALL_BLOCKED = "run-switch.uninstall-blocked"
    UNINSTALL_ISSUED_PREREQUISITE = "run-switch.uninstall-issued-prerequisite"
    UNINSTALL_TARGET_UNAVAILABLE = "run-switch.uninstall_target_unavailable"
    WAITING = "run-switch.waiting"
    REASON_UNCLASSIFIED = "run-switch.reason-unclassified"
    CANCEL_EFFECT_UNKNOWN = "run-switch.cancel-effect-unknown"
    CONTAINER_BUILD_START_UNAVAILABLE = "run-switch.container-build-start-unavailable"
    DISTRIBUTED_RECOVERY_ACTIVE = "run-switch.distributed-recovery-active"
    FINAL_OWNER_STATE_UNKNOWN = "run-switch.final-owner-state-unknown"
    FINAL_VERIFICATION_EXPIRED = "run-switch.final-verification-expired"
    INSTALL_PLAN_UNAVAILABLE = "run-switch.install-plan-unavailable"
    INSTALL_PREFLIGHT_EXPIRED = "run-switch.install-preflight-expired"
    INSTALL_PREPARATION_FAILED = "run-switch.install-preparation-failed"
    INSTALL_START_FAILED = "run-switch.install-start-failed"
    INSTALLATION_HANDOFF_INCONSISTENT = "run-switch.installation-handoff-inconsistent"
    PLAN_BLOCKED = "run-switch.plan_blocked"
    RECONCILIATION_START_FAILED = "run-switch.reconciliation-start-failed"
    ROUTE_HEALTH_RECOVERY_ACTIVE = "run-switch.route-health-recovery-active"
    ROUTE_OWNER_FAILED = "run-switch.route-owner-failed"
    ROUTE_PUBLICATION_PENDING = "run-switch.route-publication-pending"
    ROUTE_WITHDRAWN_OWNER_UNKNOWN = "run-switch.route-withdrawn-owner-unknown"
    RUN_OWNER_ACTIVE = "run-switch.run-owner-active"
    RUN_OWNER_TERMINAL = "run-switch.run-owner-terminal"
    STALE_PLAN = "run-switch.stale_plan"
    STOP_VERIFICATION_PENDING = "run-switch.stop-verification-pending"
    SUPERSEDED = "run-switch.superseded"
    UNINSTALL_ABANDON_FAILED = "run-switch.uninstall-abandon-failed"
    UNINSTALL_START_FAILED = "run-switch.uninstall-start-failed"
    RECIPE_STOP_ISSUED_PENDING = "run-switch.recipe.stop-issued-pending"
    RECIPE_INSTALL_ISSUED_PENDING = "run-switch.recipe.install-issued-pending"
    RECIPE_UNINSTALL_ISSUED_PENDING = "run-switch.recipe.uninstall-issued-pending"
    RECIPE_RECONCILE_ISSUED_PENDING = "run-switch.recipe.reconcile-issued-pending"
    ARTIFACT_JOB_CANCELLATION_ISSUED_PENDING = (
        "run-switch.artifact-job-cancellation-issued-pending"
    )
    TRANSFER_EXECUTOR_UNAVAILABLE = "run-switch.transfer-executor-unavailable"
    TRANSFER_WAITING_WITHOUT_CHILD = "run-switch.transfer-waiting-without-child"
    TRANSFER_RETURNED_NO_EVIDENCE = "run-switch.transfer-returned-no-evidence"
    VERIFY_EXECUTOR_UNAVAILABLE = "run-switch.verify-executor-unavailable"
    VERIFY_WAITING_WITHOUT_CHILD = "run-switch.verify-waiting-without-child"
    VERIFY_RETURNED_NO_EVIDENCE = "run-switch.verify-returned-no-evidence"
    CLEANUP_EXECUTOR_UNAVAILABLE = "run-switch.cleanup-executor-unavailable"
    CLEANUP_WAITING_WITHOUT_CHILD = "run-switch.cleanup-waiting-without-child"
    CLEANUP_RETURNED_NO_EVIDENCE = "run-switch.cleanup-returned-no-evidence"
    RESOURCE_CAPACITY_UNKNOWN = "run-switch.resource.capacity_unknown"
    RESOURCE_ENVELOPE_EXCEEDS_CAPACITY = "run-switch.resource.envelope_exceeds_capacity"
    RESOURCE_ENVELOPE_UNVERIFIED = "run-switch.resource.envelope_unverified"
    RESOURCE_ESTIMATE_UNCERTAIN = "run-switch.resource.estimate_uncertain"
    RESOURCE_EVIDENCE_INVALID = "run-switch.resource.evidence_invalid"
    RESOURCE_EVIDENCE_UNKNOWN = "run-switch.resource.evidence_unknown"
    RESOURCE_KNOBS_INVALID = "run-switch.resource.knobs_invalid"
    RESOURCE_PARALLELISM_DUPLICATE = "run-switch.resource.parallelism_duplicate"
    RESOURCE_PARALLELISM_INCONSISTENT = "run-switch.resource.parallelism_inconsistent"
    RESOURCE_PARALLELISM_TYPE = "run-switch.resource.parallelism_type"
    RESOURCE_PARALLELISM_UNKNOWN = "run-switch.resource.parallelism_unknown"
    RESOURCE_SETTINGS_KIND_UNKNOWN = "run-switch.resource.settings_kind_unknown"
    RESOURCE_SETTINGS_TYPE = "run-switch.resource.settings_type"
    RESOURCE_SETTINGS_UNKNOWN = "run-switch.resource.settings_unknown"
    RESOURCE_STOP_RELEASE_UNKNOWN = "run-switch.resource.stop_release_unknown"
    RECONCILE_ACTIVE_EFFECT_UNKNOWN = "run-switch.reconcile.active_effect_unknown"
    RECONCILE_AGENT_UNAVAILABLE = "run-switch.reconcile.agent_unavailable"
    RECONCILE_CAPACITY_BUSY = "run-switch.reconcile.capacity_busy"
    RECONCILE_INSTALL_PROVENANCE_MISMATCH = (
        "run-switch.reconcile.install_provenance_mismatch"
    )
    RECONCILE_INSTALL_PROVENANCE_UNAVAILABLE = (
        "run-switch.reconcile.install_provenance_unavailable"
    )
    RECONCILE_INSTALLATION_EFFECT_UNKNOWN = (
        "run-switch.reconcile.installation_effect_unknown"
    )
    RECONCILE_INSTALLATION_IDENTITY_MISMATCH = (
        "run-switch.reconcile.installation_identity_mismatch"
    )
    RECONCILE_INSTALLATION_IDENTITY_UNAVAILABLE = (
        "run-switch.reconcile.installation_identity_unavailable"
    )
    RECONCILE_MEMBERSHIP_CHANGED = "run-switch.reconcile.membership_changed"
    RECONCILE_OPERATION_ACTIVE = "run-switch.reconcile.operation_active"
    RECONCILE_RANK_MEMBERSHIP_CHANGED = "run-switch.reconcile.rank_membership_changed"
    RECONCILE_RECIPE_REVISION_UNAVAILABLE = (
        "run-switch.reconcile.recipe_revision_unavailable"
    )
    RECONCILE_SPEC_IDENTITY_MISMATCH = "run-switch.reconcile.spec_identity_mismatch"
    STOP_CAPACITY_RELEASE_DEFERRED = "run-switch.stop.capacity_release_deferred"
    STOP_RANK_MEMBERSHIP_CHANGED = "run-switch.stop.rank_membership_changed"
    STOP_RESERVATION_MEMBERSHIP_CHANGED = (
        "run-switch.stop.reservation_membership_changed"
    )
    STOP_RUN_NOT_STOPPABLE = "run-switch.stop.run_not_stoppable"
    STOP_TARGET_SCOPE_CHANGED = "run-switch.stop.target_scope_changed"
    UNINSTALL_ABANDON_NEVER_INSTALLED = "run-switch.uninstall.abandon-never-installed"
    UNINSTALL_ACTIVE_RUN = "run-switch.uninstall.active_run"
    UNINSTALL_ACTIVE_RUNS_TRUNCATED = "run-switch.uninstall.active_runs_truncated"
    UNINSTALL_BYTES_UNKNOWN = "run-switch.uninstall.bytes_unknown"
    UNINSTALL_INSTALLATION_NOT_UNINSTALLABLE = (
        "run-switch.uninstall.installation_not_uninstallable"
    )
    UNINSTALL_OPERATION_ACTIVE = "run-switch.uninstall.operation_active"
    UNINSTALL_RANK_MEMBERSHIP_CHANGED = "run-switch.uninstall.rank_membership_changed"


class StopPlanCode(WireEnum):
    """Why a stop plan is stale or cannot be taken."""

    CAPACITY_RELEASE_DEFERRED = "stop.capacity_release_deferred"
    RANK_MEMBERSHIP_CHANGED = "stop.rank_membership_changed"
    RESERVATION_MEMBERSHIP_CHANGED = "stop.reservation_membership_changed"
    RUN_NOT_STOPPABLE = "stop.run_not_stoppable"
    TARGET_SCOPE_CHANGED = "stop.target_scope_changed"


class SupersedeCode(WireEnum):
    """Why a fleet profile application was superseded by newer intent."""

    SUPERSEDED_BY_INTENT = "superseded-by-intent"
    SUPERSEDED_BY_RETRY = "superseded-by-retry"
    EFFECTS_CHANGED_DURING_ADMISSION = "effects-changed-during-admission"
