"""Run planning."""

from __future__ import annotations

from typing import (
    TYPE_CHECKING,
)
from typing import cast as typing_cast

from pydantic import ValidationError

from vonk_agent_protocol.agent_words import RunSwitchReasonScopeName
from vonk_agent_protocol.lifecycle_vocabulary import RunAdmissionCode
from vonk_agent_protocol.reason_codes.runs import RunSwitchCode
from vonk_agent_protocol.state_machines import InstallationState
from vonk_control.categorized_errors import MissingRecord
from vonk_control.content_identity import differing_image_fields
from vonk_control.fleet_profiles.capacity import (
    accepted_profile_runtime_image,
    profile_replaced_runs,
    sibling_profile_ports,
    sibling_profile_runs,
)
from vonk_control.gateway.alias_namespace import external_aliases
from vonk_control.gpu.clock_validation import assess_gpu_clock
from vonk_control.models.cache import RecipeBuild
from vonk_control.prebuilt_images import policy_prebuilt_reference
from vonk_control.preparation_contract import RuntimeImageIdentity
from vonk_control.recipe_operations.errors import RecipeOperationConflict
from vonk_control.recipe_operations.execution_contract import (
    installation_matches_runtime_image,
    parse_stored_installation_plan,
)
from vonk_control.recipe_operations.runtime_specs import RUNTIME_INTERFACE
from vonk_control.resource_planning.settings import resolve_effective_settings
from vonk_control.run_switch_contract.evidence import RunSwitchReason
from vonk_control.run_switch_contract.observation import RunSwitchObservedImageIdentity
from vonk_control.run_switch_contract.plans import RunSwitchPlan
from vonk_control.run_switch_contract.requests import RunSwitchPreviewRequest
from vonk_control.run_switch_contract.vocabulary import RunSwitchReasonScope
from vonk_control.run_switch_operations.constants import (
    _active_recipe_revision,
    _is_artifact_phase,
)
from vonk_control.run_switch_operations.identity_helpers import _primary_model_digest
from vonk_control.run_switch_operations.phase_planning import targets_hold_model
from vonk_control.run_switch_operations.planning_helpers import (
    PlanInventory,
    _as_reason,
    _now,
    _resource_reason,
    _settings_view,
)
from vonk_control.runs.admission import PORT_ADMISSION_CODES
from vonk_control.stored.states import CatalogRevisionState
from vonk_control.strict_json import read_stored_model

if TYPE_CHECKING:
    from vonk_control.run_switch_operations.service import RunSwitchOperationService

# Reasons about a NAS download, which only a load that downloads has.
_NAS_DOWNLOAD = frozenset(
    {RunSwitchCode.NAS_DOWNLOAD_BLOCKED, RunSwitchCode.NAS_DOWNLOAD_REQUIRED}
)


class RunPlanningMixin:
    def _preview_run(
        self,
        request: RunSwitchPreviewRequest,
        *,
        actor: str,
        create_build: bool = True,
        reviewed_runtime_image: RuntimeImageIdentity | None = None,
        defer_source_build: bool = False,
        excluded_profile_application_ids: tuple[str, ...] = (),
        profile_application_id: str | None = None,
        planned_ports: frozenset[tuple[str, int]] = frozenset(),
        kept_run_ids: frozenset[str] = frozenset(),
        replaced_run_ids: frozenset[str] = frozenset(),
        own_run_id: str | None = None,
    ) -> RunSwitchPlan:
        service = typing_cast("RunSwitchOperationService", self)
        now = _now(service._clock)
        group = request.spark_group
        node_ids = tuple(node.node_id for node in group.nodes)
        with service._sessions() as session:
            inventory = PlanInventory(
                session, now=now, maximum_age_seconds=service._inventory_max_age
            )
            revision = _active_recipe_revision(session, request.recipe_revision_id)
            if revision is None:
                raise MissingRecord(request.recipe_revision_id)
            expected_image = (
                accepted_profile_runtime_image(
                    session, profile_application_id, revision.id, node_ids
                )
                if profile_application_id is not None
                else reviewed_runtime_image
            )
            blockers: list[RunSwitchReason] = []
            warnings: list[RunSwitchReason] = []
            if revision.state != CatalogRevisionState.ACTIVE:
                blockers.append(
                    _as_reason(
                        RunSwitchCode.RECIPE_UNRESOLVED,
                        "The selected recipe revision is not an immutable resolved revision.",
                        scope="recipe",
                    )
                )
            recipe_model_digest = _primary_model_digest(revision.document)
            if recipe_model_digest != request.model_content_sha256:
                blockers.append(
                    _as_reason(
                        RunSwitchCode.MODEL_RECIPE_MISMATCH,
                        "The selected model variant is not the model pinned by this recipe revision.",
                        scope="model",
                    )
                )
            (
                _model_document,
                model_documents,
                document_blockers,
            ) = service._resolve_documents(
                session,
                revision,
                request.model_content_sha256,
                requested_recipe_digest=revision.content_digest,
            )
            blockers.extend(document_blockers)
            settings_resolution = resolve_effective_settings(revision.document)
            effective_settings = settings_resolution.settings
            effective_settings_view = None
            if effective_settings is None:
                blockers.extend(
                    _resource_reason(reason, node_ids=node_ids)
                    for reason in settings_resolution.reasons
                )
            else:
                effective_settings_view = _settings_view(effective_settings)
            mapping, mapping_selection, mapping_blockers = service._resolve_mapping(
                session,
                revision,
                group,
                actor=actor,
                option_choices=request.option_choices,
            )
            blockers.extend(mapping_blockers)
            placement_blockers = tuple(blockers)
            conflicts, stops, conflict_blockers = service._conflicts(
                session,
                node_ids,
                action=request.action,
                # A profile child never stops the runs its own load keeps
                # beside it, however often it is planned again.
                kept_run_ids=kept_run_ids
                | (
                    sibling_profile_runs(
                        session, profile_application_id, request.alias, node_ids
                    )
                    if profile_application_id is not None
                    else frozenset()
                ),
                # The load is the authority for every Spark its review stops a
                # run on: such a run is replaced whole, however it is placed.
                replaced_run_ids=replaced_run_ids
                | (
                    profile_replaced_runs(session, profile_application_id)
                    if profile_application_id is not None
                    else frozenset()
                ),
            )
            blockers.extend(conflict_blockers)
            # A name an external model serves is a blocker of the review, as it is
            # for a profile, not a refusal after the load was accepted.
            blockers.extend(
                _as_reason(
                    RunSwitchCode.ALIAS_TAKEN,
                    f"Model name {alias} is served by an external model.",
                    scope=typing_cast(
                        RunSwitchReasonScope, RunSwitchReasonScopeName.RECIPE.value
                    ),
                    node_ids=node_ids,
                )
                for alias in sorted(external_aliases(session, (request.alias,)))
            )
            inspection = service._inspect_artifacts(
                session,
                request.model_content_sha256,
                revision.id,
                group,
                now=now,
            )
            blockers.extend(inspection.blockers)
            warnings.extend(inspection.warnings)
            if (
                mapping_selection is not None
                and mapping_selection.action == "create"
                and service._phase_executor is None
            ):
                blockers.append(
                    _as_reason(
                        RunSwitchCode.MAPPING_MATERIALIZATION_UNAVAILABLE,
                        "No phase executor is configured to materialize a new exact Spark mapping.",
                        scope="mapping",
                        node_ids=node_ids,
                    )
                )
            installation = service._matching_installation(
                session,
                revision.id,
                request.model_content_sha256,
                mapping,
                group,
            )
            # An installed installation that already runs the needed image on
            # every target Spark starts as it is: a build row or a NAS archive
            # is provenance, not identity, so neither is consulted. Its
            # installed ranks name the image, so every target holds it. Model
            # bytes missing on a Spark need the transfer phases, so that load
            # keeps the full resolution.
            installed_plan = (
                parse_stored_installation_plan(installation.plan)
                if installation is not None
                else None
            )
            installed_image = (
                service._installed_image(
                    session,
                    installation,
                    installed_plan,
                    revision,
                    expected_image=expected_image,
                )
                if installation is not None
                and installed_plan is not None
                and inspection.missing_spark_bytes in (None, 0)
                else None
            )
            build_selection = None
            build = None
            build_candidate = None
            reuse_installed = installed_image is not None
            if installed_image is not None:
                build_evidence, runtime_storage, build_blockers, build_warnings = (
                    service._build_evidence(
                        session,
                        revision,
                        None,
                        None,
                        group,
                        expected_image=installed_image,
                        require_available=False,
                        image_installed=True,
                    )
                )
            else:
                build = service._matching_build(
                    session,
                    revision.id,
                    expected_image=expected_image,
                )
                build_candidate = build or (
                    session.get(RecipeBuild, expected_image.build_id)
                    if expected_image is not None
                    and expected_image.build_id is not None
                    else service._latest_build(session, revision.id)
                )
                build_selection = service._select_build(
                    session,
                    revision,
                    build,
                    build_candidate,
                    group,
                    inventory=inventory,
                    create_build=create_build,
                )
                build = build_selection.build
                build_candidate = build_selection.candidate
                if installation is not None and (
                    build is None
                    or not installation_matches_runtime_image(
                        installation,
                        image_digest=build.image_digest,
                        oci_layout_sha256=build.oci_layout_sha256,
                        image_bytes=build.image_bytes,
                        plan=installed_plan,
                    )
                ):
                    # A replacement image needs a fresh compiled plan and
                    # install receipt.
                    installation = None
                blockers.extend(build_selection.blockers)
                build_evidence, runtime_storage, build_blockers, build_warnings = (
                    service._build_evidence(
                        session,
                        revision,
                        build,
                        build_candidate,
                        group,
                        defer_source_build=defer_source_build,
                        expected_image=expected_image,
                    )
                )
            blockers.extend(build_blockers)
            warnings.extend(build_warnings)
            resource_fits = service._resource_fits(
                session,
                revision,
                request,
                now=now,
                inventory=inventory,
                stops=stops,
                placement_blockers=placement_blockers,
                effective_settings=effective_settings,
                model_documents=model_documents,
                image_bytes=(
                    runtime_storage.image_bytes
                    if runtime_storage.image_bytes is not None
                    else expected_image.image_bytes
                    if expected_image is not None
                    else None
                ),
                artifact_bytes=inspection.artifact_set_bytes,
                artifact_set_sha256=inspection.artifact_set_sha256,
                missing_artifact_bytes_by_node=inspection.missing_spark_bytes_by_node,
                excluded_profile_application_ids=excluded_profile_application_ids,
                planned_ports=planned_ports
                | (
                    sibling_profile_ports(
                        session, profile_application_id, revision.id, node_ids
                    )
                    if profile_application_id is not None
                    else frozenset()
                ),
                kept_run_ids=kept_run_ids,
                own_run_id=own_run_id,
                profile_application_id=profile_application_id,
            )
            freshness = resource_fits.freshness
            fit_current = resource_fits.current
            fit_after_stop = resource_fits.after_stop
            blockers.extend(resource_fits.blockers)
            warnings.extend(resource_fits.warnings)
            builder_freshness = (
                build_selection.builder_freshness
                if build_selection is not None
                else None
            )
            if builder_freshness is not None and not any(
                item.source == builder_freshness.source for item in freshness
            ):
                freshness.append(builder_freshness)
            start_plan_digest: str | None = None
            recipe_build_id = (
                installed_image.build_id
                if installed_image is not None
                else build.id
                if build is not None
                else build_candidate.id
                if build_candidate is not None
                and (
                    build_candidate.state in {"planned", "building"}
                    or expected_image is not None
                )
                else None
            )
            image_digest = (
                build.image_digest
                if build is not None
                else installation.image_digest
                if reuse_installed and installation is not None
                else expected_image.image_digest
                if expected_image is not None
                else runtime_storage.image_digest
            )
            if (
                expected_image is not None
                and profile_application_id is not None
                and runtime_storage.image_digest is not None
            ):
                changes = differing_image_fields(
                    expected_image,
                    RunSwitchObservedImageIdentity(
                        image_digest=runtime_storage.image_digest,
                        oci_layout_sha256=runtime_storage.oci_layout_sha256,
                        image_bytes=runtime_storage.image_bytes,
                        build_id=recipe_build_id,
                        architecture="linux-arm64",
                        runtime_interface=RUNTIME_INTERFACE,
                    ),
                )
                if changes:
                    blockers.append(
                        _as_reason(
                            RunSwitchCode.RUNTIME_IMAGE_PREPARATION_RECEIPT_INVALID,
                            "Accepted runtime image observation differs: "
                            + ", ".join(changes),
                            scope="artifact",
                            node_ids=node_ids,
                        )
                    )
            if installation is None:
                if service._phase_executor is None:
                    blockers.append(
                        _as_reason(
                            RunSwitchCode.INSTALLATION_PREPARATION_UNAVAILABLE,
                            "No phase executor is configured to prepare this exact recipe on the selected group.",
                            scope="operation",
                            node_ids=node_ids,
                        )
                    )
            elif (
                request.action != "install"
                and installation.state == InstallationState.INSTALLED
                and service._lifecycle is not None
                # A workload the plan keeps already serves: nothing starts.
                and not resource_fits.already_kept
            ):
                # The runs this plan stops release their reservations in the
                # Stop phase, so admission must not count that capacity against
                # the replacement's own preview.  Counting it refused the plan
                # for the workload it was replacing, and since the only release
                # path is a successful stop, nothing could break the tie.
                planned_stop_ids = frozenset(stop.run_id for stop in stops)
                try:
                    low_level_plan = service._lifecycle.preview_run(
                        installation.id,
                        request.alias,
                        excluded_profile_application_ids=excluded_profile_application_ids,
                        released_run_ids=planned_stop_ids,
                        kept_run_ids=resource_fits.kept_run_ids,
                    )
                except ValidationError:
                    # A freshly built contract defect belongs to assessment,
                    # not the availability blocker below.
                    raise
                except (
                    KeyError,
                    RecipeOperationConflict,
                    RuntimeError,
                    TypeError,
                    ValueError,
                ) as error:
                    blockers.append(
                        _as_reason(
                            RunSwitchCode.RUN_ADMISSION_UNAVAILABLE,
                            f"The exact run admission plan could not be produced: {error}",
                            scope="operation",
                            node_ids=node_ids,
                        )
                    )
                else:
                    start_plan_digest = low_level_plan.plan_digest
                    remaining_admission_blockers = False
                    for item in low_level_plan.nodes:
                        for reason in item.blockers:
                            if reason.code in PORT_ADMISSION_CODES:
                                # The shared fit checks these for fresh and existing
                                # installs and excludes only exact reviewed stops.
                                continue
                            if (
                                reason.code
                                in {
                                    RunAdmissionCode.INSUFFICIENT_MEMORY,
                                    RunAdmissionCode.INSUFFICIENT_SWAP,
                                }
                                and resource_fits.post_stop_memory_check is not None
                            ):
                                # The reviewed exact stops require a fresh
                                # post-stop fit; this pre-stop result cannot
                                # establish the later physical capacity.
                                continue
                            remaining_admission_blockers = True
                            blockers.append(
                                _as_reason(
                                    reason.code,
                                    reason.detail,
                                    scope="node",
                                    node_ids=(item.node_id,),
                                    swap=reason.swap,
                                )
                            )
                        warnings.extend(
                            _as_reason(
                                reason.code,
                                reason.detail,
                                scope="node",
                                severity="warning",
                                node_ids=(item.node_id,),
                            )
                            for reason in item.warnings
                        )
                    if remaining_admission_blockers:
                        blockers.append(
                            _as_reason(
                                RunSwitchCode.RUN_ADMISSION_BLOCKED,
                                "The existing run admission primitive rejected one or more selected ranks.",
                                scope="operation",
                                node_ids=node_ids,
                            )
                        )
            stop_before_prepare = resource_fits.stop_before_prepare
            stop_before_transfer = resource_fits.stop_before_transfer
            if targets_hold_model(inspection):
                # No download is planned, so whether the NAS could take one
                # neither blocks this load nor is announced to it.
                blockers[:] = [r for r in blockers if r.code not in _NAS_DOWNLOAD]
                warnings[:] = [r for r in warnings if r.code not in _NAS_DOWNLOAD]
            phases = service._phases(
                action=request.action,
                group=group,
                installation_id=installation.id if installation is not None else None,
                installation_state=installation.state
                if installation is not None
                else None,
                stops=stops,
                inspection=inspection,
                runtime_storage=runtime_storage,
                blockers=blockers,
                stop_before_transfer=stop_before_transfer,
                stop_before_prepare=stop_before_prepare,
                build_required=(
                    build is None
                    and build_candidate is not None
                    and build_candidate.state in {"planned", "building"}
                ),
                build_on_target=(
                    build is None
                    and build_candidate is not None
                    and build_candidate.state in {"planned", "building"}
                    and build_candidate.builder_node_id in node_ids
                    # A prebuilt image is pulled by the Controller; the
                    # nominal builder Spark does no work and needs no memory.
                    and policy_prebuilt_reference(build_candidate.policy_report) is None
                ),
            )
            if (
                not service._custom_phase_executor
                and service._artifact_phase_executor is None
                and any(
                    _is_artifact_phase(phase.kind)
                    or (phase.kind == "prepare" and phase.subphase == "runtime-image")
                    for phase in phases
                )
            ):
                blockers.append(
                    _as_reason(
                        RunSwitchCode.ARTIFACT_PHASE_EXECUTOR_UNAVAILABLE,
                        "Artifact transfer, verification, or Controller image preparation requires an injected cache boundary.",
                        scope="artifact",
                        node_ids=node_ids,
                    )
                )
                phases = service._phases(
                    action=request.action,
                    group=group,
                    installation_id=installation.id
                    if installation is not None
                    else None,
                    installation_state=installation.state
                    if installation is not None
                    else None,
                    stops=stops,
                    inspection=inspection,
                    runtime_storage=runtime_storage,
                    blockers=blockers,
                    stop_before_transfer=stop_before_transfer,
                    stop_before_prepare=stop_before_prepare,
                    build_required=(
                        build is None
                        and build_candidate is not None
                        and build_candidate.state in {"planned", "building"}
                    ),
                    build_on_target=(
                        build is None
                        and build_candidate is not None
                        and build_candidate.state in {"planned", "building"}
                        and build_candidate.builder_node_id in node_ids
                        and policy_prebuilt_reference(build_candidate.policy_report)
                        is None
                    ),
                )
            if request.gpu_clock is not None:
                clock_blockers, clock_warnings = assess_gpu_clock(
                    node_ids, request.gpu_clock
                )
                blockers.extend(clock_blockers)
                warnings.extend(clock_warnings)
            preparation = service._preparation(
                revision=revision,
                group=group,
                inspection=inspection,
                build=build,
                build_candidate=build_candidate,
                runtime_storage=runtime_storage,
                now=now,
                reasons=[*blockers, *warnings],
            )
            storage = service._storage(inspection)
            plan_data = read_stored_model(
                RunSwitchPlan,
                {
                    "schema_version": 2,
                    "generated_at": now,
                    "action": request.action,
                    "model_content_sha256": request.model_content_sha256,
                    "recipe_revision_id": revision.id,
                    "recipe_content_sha256": revision.content_digest,
                    "alias": request.alias,
                    "run_id": None,
                    "spark_group": group,
                    "gpu_clock": request.gpu_clock,
                    "mapping": mapping_selection,
                    "installation_id": installation.id
                    if installation is not None
                    else None,
                    "installation_state": installation.state
                    if installation is not None
                    else None,
                    "recipe_build_id": recipe_build_id,
                    "image_digest": image_digest,
                    "start_plan_digest": start_plan_digest,
                    "freshness": freshness,
                    "fit_current": fit_current,
                    "fit_after_stop": fit_after_stop,
                    "post_stop_memory_check": resource_fits.post_stop_memory_check,
                    "fit": fit_current,
                    "effective_settings": effective_settings_view,
                    "storage": storage,
                    "runtime_storage": runtime_storage,
                    "build": build_evidence,
                    "preparation": preparation,
                    "conflicts": conflicts,
                    "stops": stops,
                    "phases": phases,
                    "allowed": not blockers,
                    "blockers": blockers,
                    "warnings": warnings,
                    "invocation": request.invocation,
                    "plan_digest": "0" * 64,
                    "stop_before_prepare": stop_before_prepare,
                    "stop_before_transfer": stop_before_transfer,
                },
            )
            return service._finalize_plan(plan_data)
