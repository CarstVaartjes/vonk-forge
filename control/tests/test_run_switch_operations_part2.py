from __future__ import annotations

import copy
import hashlib
import uuid
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select
from vonk_agent_protocol import (
    InstallationState,
    canonical_message,
)
from vonk_control.cluster_mappings import ClusterMappingService
from vonk_control.distribution_executor.receipts import _ChildView
from vonk_control.execution_plan_service import ControllerExecutionPlanService
from vonk_control.models import (
    AgentNode,
    AgentOperation,
    CatalogDocumentRevision,
    Job,
    NodeInventorySnapshot,
    RecipeBuild,
    RecipeInstallation,
    RecipeSourceBundle,
    ResourceReservation,
)
from vonk_control.recipe_builds import RecipeBuildPlan
from vonk_control.recipe_builds.cancellation import (
    lock_build_dependency,
)
from vonk_control.recipe_operations import (
    RecipeOperationView,
)
from vonk_control.recipe_runtime_specs import (
    compile_runtime_spec,
    resolve_recipe_entities,
    split_option_choices,
)
from vonk_control.run_switch_contract import (
    RunSwitchApplyRequest,
    RunSwitchPreviewRequest,
    SparkGroup,
    SparkGroupNode,
)
from vonk_control.run_switch_operations import (
    PhaseExecution,
    RecipeLifecyclePhaseExecutor,
    RunSwitchOperationProvider,
    RunSwitchOperationService,
    _build_receipt_in_session,
    effective_build_receipt,
)
from vonk_control.runtime.preflight import latest_result
from vonk_control.runtime_image_preparation import (
    FilesystemRuntimeImageStorage,
    PulledImageEvidence,
    prepare_runtime_image,
)
from vonk_forge_contracts import RecipeDefinition, document_sha256

from .display_fixtures import reword
from .preflight_fixtures import record_passing_preflight
from .recipe_operations_support import (
    NOW,
    _CanonicalModelCache,
    installed_recipe,
    setup_services,
)
from .run_switch_operations_support import (
    CompleteArtifactInspector,
    RecordingArtifactExecutor,
    _cold_compile_switch,
    _fixture_execution,
    _host_changes_during_compile,
    _installed_service,
    _phase_kinds,
    _replace_child,
    _request,
    _runtime_receipt,
    _target_copy_evidence,
)
from .runtime_identity_support import (
    set_inventory_fingerprint,
    set_inventory_identity,
)
from .runtime_image_fixtures import place_test_image
from .test_lifecycle_preflight import _clear, _finish


class BuildThenCopyExecutor:
    """Drive the real build phase and retain a durable child for the test."""

    def __init__(self, lifecycle, sessions) -> None:
        self.children: dict[str, _ChildView] = {}
        self.build_preview_calls = 0
        self.build_start_calls = 0
        self.receipts: list[object] = []
        self._lifecycle = lifecycle
        self._sessions = sessions
        self._delegate = RecipeLifecyclePhaseExecutor(
            lifecycle,
            sessions,
            ClusterMappingService(sessions),
            lambda: NOW,
        )

    def execute(
        self,
        plan,
        phase,
        *,
        item_index,
        actor,
        request_key,
        progress,
    ) -> PhaseExecution:
        if phase.subphase == "container-build":
            self.build_preview_calls += 1
            execution = self._delegate.execute(
                plan,
                phase,
                item_index=item_index,
                actor=actor,
                request_key=request_key,
                progress=progress,
            )
            if execution.operation_id is not None:
                assert execution.result is not None
                self.children[execution.operation_id] = _ChildView(
                    state="running", result=execution.result
                )
            return execution
        if phase.subphase == "target-copy":
            self.receipts.append(effective_build_receipt(plan, progress))
            return _fixture_execution(
                plan, phase, result=_target_copy_evidence(plan, phase, progress)
            )
        if phase.subphase == "runtime-image":
            with self._sessions() as session:
                build = session.get(RecipeBuild, plan.recipe_build_id)
                assert build is not None
                assert build.image_digest is not None
                assert build.oci_layout_sha256 is not None
                assert build.image_bytes is not None
                return _fixture_execution(
                    plan,
                    phase,
                    result={
                        "runtime_image": _runtime_receipt(
                            plan,
                            image=build.image_digest,
                            layout=build.oci_layout_sha256,
                            size=build.image_bytes,
                            build_id=build.id,
                        ),
                        "image_digest": build.image_digest,
                        "oci_layout_sha256": build.oci_layout_sha256,
                        "image_bytes": build.image_bytes,
                    },
                )
        if phase.subphase in {"runtime-plan", "runtime-install"}:
            if phase.subphase == "runtime-plan":
                return _fixture_execution(
                    plan,
                    phase,
                    result={
                        "installation_id": str(uuid.uuid4()),
                        "mapping_id": str(uuid.uuid4()),
                        "install_plan_digest": plan.plan_digest,
                        "compiled_plan_persisted": True,
                    },
                )
            return _fixture_execution(
                plan, phase, result={"installation_id": str(uuid.uuid4())}
            )
        return _fixture_execution(plan, phase, result={"phase": phase.kind})

    def get(self, operation_id: str) -> _ChildView:
        return self.children[operation_id]


def test_cold_production_phases_prepare_receipts_before_real_install_compile(
    tmp_path: Path,
) -> None:
    """Exercise the real image prep and install compiler after cold preview."""

    sessions, lifecycle, _queue, _mapping_id, build_id, nodes = setup_services(tmp_path)
    with sessions.begin() as session:
        build = session.get(RecipeBuild, build_id)
        assert build is not None
        image_digest = build.image_digest
        layout_digest = build.oci_layout_sha256
        image_bytes = build.image_bytes
        revision_id = build.recipe_revision_id
    assert image_digest is not None
    assert layout_digest is not None
    assert image_bytes is not None

    controller_storage = FilesystemRuntimeImageStorage(
        tmp_path / "controller-artifacts"
    )
    expected_archive = b"canonical-runtime-image-archive"[:image_bytes]
    assert hashlib.sha256(expected_archive).hexdigest() == layout_digest
    place_test_image(controller_storage, layout_digest, len(expected_archive))

    # Compilation needs the model's identity, not its NAS copy.
    model_cache = _CanonicalModelCache()

    class _BuildArchiveTransport:
        def pull_and_export(
            self,
            reference: str,
            destination: Path,
            *,
            expected_architecture: str,
            expected_runtime_interface: str,
            progress=None,
        ) -> PulledImageEvidence:
            raise AssertionError("the test runtime image archive is already stored")

        def inspect_archive(
            self,
            archive: Path,
            *,
            expected_architecture: str,
            expected_runtime_interface: str,
            expected_archive_sha256: str,
            expected_archive_bytes: int,
        ) -> PulledImageEvidence:
            return PulledImageEvidence(
                manifest_digest=image_digest,
                config_id="sha256:" + "4" * 64,
                local_reference="oci-archive:" + str(archive),
                architecture=expected_architecture,
                runtime_interface="v1",
                archive_sha256=expected_archive_sha256,
                archive_bytes=expected_archive_bytes,
            )

    transport = _BuildArchiveTransport()

    def resolve_runtime_image(document, requested_digest, runtime_spec):
        assert requested_digest == image_digest
        runtime = runtime_spec.runtime
        receipt = controller_storage.find_verified(
            requested_digest,
            expected_architecture=runtime.architecture,
            expected_runtime_interface=runtime.interface,
        )
        if receipt is None:
            raise RuntimeError("verified runtime image receipt is unavailable")
        return receipt

    execution_plans = ControllerExecutionPlanService(
        model_cache,
        runtime_image_resolver=resolve_runtime_image,
    )
    # Keep the final compile strict/read-only.  The worker phase below is the
    # only code that writes the cold model/image receipts first.
    lifecycle._install_admission._compiled_plan_provider = (
        execution_plans.compile_installation
    )

    class ColdInspector(CompleteArtifactInspector):
        def inspect(self, *args, **kwargs):
            value = super().inspect(*args, **kwargs)
            return replace(
                value,
                missing_nas_bytes=1024,
                nas_coverage="partial",
            )

    class ProductionColdExecutor:
        def __init__(self) -> None:
            self.events: list[str] = []
            self.delegate = RecipeLifecyclePhaseExecutor(
                lifecycle,
                sessions,
                ClusterMappingService(sessions),
                lambda: NOW,
            )

        def execute(
            self,
            plan,
            phase,
            *,
            item_index,
            actor,
            request_key,
            progress,
        ):
            identity = phase.subphase or phase.kind
            self.events.append(identity)
            if phase.subphase == "model-download":
                return _fixture_execution(
                    plan,
                    phase,
                    result={
                        "schema_version": 2,
                        "artifact_set_sha256": plan.preparation.model.artifact_set_sha256,
                        "coverage": "complete",
                        "downloaded_bytes": plan.storage.missing_nas_bytes,
                        "total_bytes": plan.storage.missing_nas_bytes,
                        "progress": {
                            "phase": "model-download",
                            "completed_bytes": plan.storage.missing_nas_bytes,
                            "total_bytes": plan.storage.missing_nas_bytes,
                            "total_bytes_known": True,
                        },
                    },
                )
            if phase.subphase == "runtime-image":
                with sessions() as session:
                    revision = session.get(CatalogDocumentRevision, revision_id)
                    build = session.get(RecipeBuild, build_id)
                    assert revision is not None and build is not None
                    entities = resolve_recipe_entities(session, revision)
                    option_choices, settings = split_option_choices(
                        plan.mapping.parameters if plan.mapping is not None else None
                    )
                    runtime_spec = compile_runtime_spec(
                        entities.recipe,
                        recipe_digest=entities.recipe_digest,
                        models=entities.models,
                        parameters=settings,
                        option_choices=option_choices,
                        role=plan.spark_group.nodes[0].role,
                        rank=plan.spark_group.nodes[0].rank,
                        package_handle={
                            "image_digest": build.image_digest,
                            "image_reference": f"localhost/vonk/recipe-build@{build.image_digest}",
                            "build_input_sha256": build.build_input_sha256,
                            "platform": "linux/arm64",
                        },
                    )
                    receipt = prepare_runtime_image(
                        revision.document,
                        runtime=runtime_spec.runtime.document(),
                        storage=controller_storage,
                        transport=transport,
                        build_receipt={
                            "state": build.state,
                            "build_id": build.id,
                            "image_digest": build.image_digest,
                            "oci_layout_sha256": build.oci_layout_sha256,
                            "image_bytes": build.image_bytes,
                        },
                        now=NOW,
                    )
                return _fixture_execution(
                    plan,
                    phase,
                    result={
                        "runtime_image": receipt.to_mapping(),
                        "image_digest": receipt.image_digest,
                        # Keep the phase result's established wire key; the
                        # preparation receipt itself uses its established
                        # ``oci_archive_sha256`` field.
                        "oci_layout_sha256": receipt.oci_archive_sha256,
                        "image_bytes": receipt.image_bytes,
                    },
                )
            if phase.subphase in {"runtime-plan", "runtime-install"}:
                return self.delegate.execute(
                    plan,
                    phase,
                    item_index=item_index,
                    actor=actor,
                    request_key=request_key,
                    progress=progress,
                )
            if phase.kind == "transfer" and phase.subphase == "target-copy":
                return _fixture_execution(
                    plan, phase, result=_target_copy_evidence(plan, phase)
                )
            if phase.kind == "verify":
                return _fixture_execution(
                    plan,
                    phase,
                    result={
                        "verified": True,
                        "verified_build_id": getattr(plan, "recipe_build_id", None),
                        "verified_image_digest": plan.image_digest,
                        "verified_oci_layout_sha256": plan.build.oci_layout_sha256,
                    },
                )
            return _fixture_execution(plan, phase, result={"phase": phase.kind})

        def get(self, operation_id: str):
            return self.delegate.get(operation_id)

    executor = ProductionColdExecutor()
    service = RunSwitchOperationService(
        sessions,
        lifecycle=lifecycle,
        clock=lambda: NOW,
        artifacts=ColdInspector(missing_spark_bytes=1024),
        phase_executor=executor,
        artifact_phase_executor=executor,
        memory_floor_bytes=50,
    )
    request = _request(sessions, nodes[0])
    plan = service.preview(request, actor="admin")
    assert plan.allowed, [reason.code for reason in plan.blockers]
    assert not controller_storage.find_verified(
        image_digest,
        expected_architecture="linux/arm64",
        expected_runtime_interface="vonk.runtime.v1",
    )

    request_key = str(uuid.uuid4())
    operation = service.apply(
        RunSwitchApplyRequest(
            **request.model_dump(),
            plan_digest=plan.plan_digest,
            request_key=request_key,
        ),
        actor="admin",
    )
    replay = service.apply(
        RunSwitchApplyRequest(
            **request.model_dump(),
            plan_digest=plan.plan_digest,
            request_key=request_key,
        ),
        actor="admin",
    )
    assert replay.operation_id == operation.operation_id
    assert service.tick() is True
    assert service.tick() is True
    assert executor.events == ["model-download", "runtime-image"]
    assert (
        controller_storage.find_verified(
            image_digest,
            expected_architecture="linux/arm64",
            expected_runtime_interface="vonk.runtime.v1",
        )
        is not None
    )

    # Controller compilation/persistence is synchronous and must not create a
    # Spark install child before target-copy verification.
    assert service.tick() is True
    planned = service.get(operation.operation_id)
    assert planned.progress.subphase == "target-copy"
    assert planned.result is not None
    assert planned.result.child_operation_id is None
    with sessions() as session:
        installation = session.scalar(
            select(RecipeInstallation).where(
                RecipeInstallation.recipe_build_id == build_id,
            )
        )
        assert installation is not None, list(
            session.scalars(select(RecipeInstallation))
        )
        compiled_plans = installation.plan["compiled_execution_plans"]
        assert isinstance(compiled_plans, dict)
        assert compiled_plans
    assert executor.events == ["model-download", "runtime-image", "runtime-plan"]

    assert service.tick() is True
    assert service.get(operation.operation_id).progress.subphase == "target-copy"
    assert service.tick() is True
    assert service.get(operation.operation_id).progress.subphase == "runtime-install"
    install_stage = service.get(operation.operation_id)
    assert install_stage.result is not None
    assert install_stage.result.child_operation_id is None
    assert service.tick() is True
    waiting = service.get(operation.operation_id)
    assert waiting.progress.subphase == "runtime-install"
    assert waiting.result is not None
    install_child_id = waiting.result.child_operation_id
    assert install_child_id
    assert executor.events == [
        "model-download",
        "runtime-image",
        "runtime-plan",
        "target-copy",
        "target-copy",
        "runtime-install",
    ]

    lifecycle.record_node_result(
        install_child_id,
        nodes[0],
        succeeded=True,
        evidence={"installed_bytes": 1024},
    )
    assert service.tick() is True
    assert service.get(operation.operation_id).progress.subphase in {
        "start",
        None,
    }


def _runtime_probes(sessions) -> int:
    from vonk_agent_protocol import AgentOperation as AgentOperationKind

    with sessions() as session:
        return len(
            list(
                session.scalars(
                    select(AgentOperation.id).where(
                        AgentOperation.kind
                        == AgentOperationKind.RUNTIME_PREFLIGHT.value
                    )
                )
            )
        )


def test_a_switch_longer_than_five_minutes_runs_one_preflight_round(
    tmp_path: Path,
) -> None:
    """A receipt is valid by identity, never by age.

    Catches the wall-clock window: a cold ``prepare/runtime-plan`` compile
    measured 716 s, after which install admission re-aged the receipt past
    300 s, held the switch for ``install-preflight-refresh`` and ran a second
    probe round on an unchanged host.  Every switch whose phases outlast five
    minutes paid that round (15-130 s per load).  Same requirements digest and
    same host fingerprint: the receipt from the one round still admits.
    """

    switch = _cold_compile_switch(tmp_path)
    clock, admission = switch.clock, switch.admission
    probes = _runtime_probes(switch.sessions)

    for _ in range(8):
        if "runtime-plan" in switch.executor.events:
            break
        switch.drive()
    assert "runtime-plan" in switch.executor.events, switch.executor.events
    assert (clock.now - NOW).total_seconds() == 716

    replanned = admission.plan_install(
        switch.mapping_id, switch.build_id, now=clock.now
    )
    assert replanned.allowed, [
        reason.code for node in replanned.nodes for reason in node.blockers
    ]
    assert replanned.plan_digest == switch.admitted.plan_digest

    for _ in range(40):
        if switch.service.get(switch.operation.operation_id).state != "running":
            break
        switch.drive()
    with switch.sessions() as session:
        installations = list(session.scalars(select(RecipeInstallation)))
    assert len(installations) == 1
    assert installations[0].plan_digest == switch.admitted.plan_digest
    completed = switch.service.get(switch.operation.operation_id)
    assert completed.result is not None and completed.result.retry_attempt is None
    assert completed.progress.operation.phase != "install-preflight-refresh"
    assert _runtime_probes(switch.sessions) == probes


def test_agent_upgrade_during_runtime_install_preflight_completes_the_switch(
    tmp_path: Path,
) -> None:
    """An agent upgrade between a probe's issue and result is not a host change.

    The first claim of the new agent stores a new build digest and fingerprint
    before the daemon-reload; the probe then reports the host after the reload.
    The probe is the newest host sample, so the gate admits it with no extra
    round and the switch still creates exactly one installation.
    """

    from vonk_agent_protocol import AgentOperation as AgentOperationKind

    switch = _cold_compile_switch(tmp_path, seconds=1, slow_compiles=0)
    sessions, clock, service = switch.sessions, switch.clock, switch.service
    operation_id = switch.operation.operation_id

    def probes() -> int:
        with sessions() as session:
            return len(
                list(
                    session.scalars(
                        select(AgentOperation).where(
                            AgentOperation.kind
                            == AgentOperationKind.RUNTIME_PREFLIGHT.value
                        )
                    )
                )
            )

    for _ in range(40):
        view = service.get(operation_id)
        if view.progress.subphase == "runtime-install":
            break
        switch.drive()
    # The receipts gathered so far are gone, so the runtime-install gate probes.
    _clear(sessions)

    upgraded_at: list[int] = []
    for _ in range(40):
        before = service.get(operation_id)
        due = before.next_attempt_at if before.result is not None else None
        if due is not None and due > clock.now:
            clock.now = due
        service.tick()
        view = service.get(operation_id)
        assert view.result is not None
        checkpoint = view.result.preflight
        pending_id = (
            None
            if checkpoint is None
            else next(iter(checkpoint.pending_jobs.values()), None)
        )
        if pending_id is not None:
            with sessions() as session:
                pending = session.get(Job, pending_id)
                answerable = pending is not None and pending.state in {
                    "queued",
                    "running",
                }
            if answerable and view.progress.subphase == "runtime-install":
                assert not upgraded_at
                upgraded_at.append(probes())
                with sessions.begin() as session:
                    for node_id in switch.nodes:
                        node = session.get(AgentNode, node_id)
                        set_inventory_identity(node, build_digest="sha256:" + "d" * 64)
                        set_inventory_fingerprint(node, "b" * 64)
                _finish(sessions, checkpoint, clock.now, fingerprint="c" * 64)
            elif answerable:
                _finish(sessions, checkpoint, clock.now)
        if "runtime-install" in switch.executor.events:
            break
    assert upgraded_at, "the runtime-install gate never issued a probe"
    assert "runtime-install" in switch.executor.events, switch.executor.events
    assert probes() == upgraded_at[0]

    for _ in range(20):
        if service.get(operation_id).state != "running":
            break
        switch.drive()
    completed = service.get(operation_id)
    assert completed.state != "failed", completed.status_reason
    assert completed.result is not None and completed.result.retry_attempt is None
    assert completed.progress.operation.phase != "install-preflight-refresh"
    with sessions() as session:
        assert len(list(session.scalars(select(RecipeInstallation)))) == 1


def test_runtime_install_capacity_busy_parks_and_retries_the_switch(
    tmp_path: Path,
) -> None:
    """A busy install admission remains recoverable at child dispatch."""

    switch = _cold_compile_switch(tmp_path, runtime_install_busy_attempts=1)
    for _ in range(20):
        switch.drive()
        view = switch.service.get(switch.operation.operation_id)
        if (
            view.result is not None
            and view.result.retry_reason == "install.capacity_busy"
        ):
            break
    else:
        pytest.fail("runtime install capacity wait was not observed")

    assert view.state == "running"
    assert view.progress.subphase == "runtime-install"
    assert view.result is not None
    assert view.next_attempt_at is not None

    for _ in range(20):
        switch.drive()
        if switch.executor.events.count("runtime-install") >= 2:
            break
    assert switch.executor.events.count("runtime-install") == 2
    recovered = switch.service.get(switch.operation.operation_id)
    assert recovered.state != "failed"
    assert recovered.result is not None and recovered.result.retry_attempt is None


def test_run_switch_retry_reports_its_wait_and_is_not_failed(
    tmp_path: Path, caplog
) -> None:
    """A retrying switch shows what it waits for and when, never "failed"."""

    switch = _cold_compile_switch(tmp_path, runtime_install_busy_attempts=1)
    with caplog.at_level("INFO"):
        for _ in range(20):
            switch.drive()
            view = switch.service.get(switch.operation.operation_id)
            if view.blockers:
                break
        else:
            pytest.fail("the retry was never reported as a wait")

    assert view.state != "failed"
    # Recovery is asserted through effects and ownership below.
    assert view.blockers[0].node_ids
    assert view.next_attempt_at == view.next_attempt_at
    item = RunSwitchOperationProvider(switch.service).get_operation(
        switch.operation.operation_id
    )
    assert item.blockers is not None
    # Recovery is asserted through effects and ownership below.
    assert item.next_attempt_at is not None
    assert sum("is waiting" in line for line in caplog.messages) == 1

    for _ in range(20):
        switch.drive()
        if switch.executor.events.count("runtime-install") >= 2:
            break
    recovered = switch.service.get(switch.operation.operation_id)
    assert recovered.blockers == [] and recovered.next_attempt_at is None


def test_runtime_install_capacity_wait_backs_off_and_resets_after_progress(
    tmp_path: Path,
) -> None:
    switch = _cold_compile_switch(tmp_path)
    # One retry clock: the lifecycle core's stable jittered backoff (2..60 s).
    expected_caps = [2, 4, 8, 16]
    observed_delays: list[int] = []
    for cap in expected_caps:
        current = switch.service.get(switch.operation.operation_id)
        assert current.result is not None
        assert switch.service._hold_capacity_writer(
            switch.operation.operation_id,
            current.result.phase_index,
            current.result.item_index,
            reason="install.capacity_busy",
        )
        view = switch.service.get(switch.operation.operation_id)
        assert view.state == "running"
        assert view.result is not None and view.next_attempt_at is not None
        due = view.next_attempt_at
        observed_delays.append(int((due - switch.clock.now).total_seconds()))
        assert max(1, cap * 3 // 4) <= observed_delays[-1] <= min(60, cap * 5 // 4)
        assert view.result.retry_attempt == len(observed_delays) + 1
        switch.clock.now = due


def test_preflight_refresh_after_repeated_cold_compiles_ends_and_admits_fresh(
    tmp_path: Path,
) -> None:
    """A host that changes during every compile ends the refresh; fresh intent runs."""
    from vonk_control.run_switch_operations import _run_switch_payload

    from .non_blocking import assert_ended_without_blocking

    switch = _cold_compile_switch(tmp_path, slow_compiles=99)
    _host_changes_during_compile(switch)
    service, operation = switch.service, switch.operation
    with switch.sessions() as session:
        from vonk_control.jobs.documents import RunSwitchRunIntent

        payload = _run_switch_payload(session.get(Job, operation.operation_id))
        assert payload is not None and isinstance(payload.intent, RunSwitchRunIntent)
        request = payload.intent.request

    def end(_operation):
        for _ in range(60):
            view = service.get(operation.operation_id)
            if view.result.failed_phase is not None:
                break
            switch.drive()
        else:
            pytest.fail("persistent preflight did not end within its request budget")
        assert "runtime-install" not in switch.executor.events
        with switch.sessions() as session:
            assert not list(session.scalars(select(RecipeInstallation)))
        return view

    def fresh(_world):
        switch.compiler._slow_compiles = 0
        return service.apply(
            RunSwitchApplyRequest.model_validate_json(
                canonical_message(
                    request.model_copy(
                        update={"request_key": str(uuid.uuid4()), "plan_digest": None}
                    )
                )
            ),
            actor="admin",
        )

    def cause(receipt):
        assert receipt.result.failure_code is not None

    _, admitted = assert_ended_without_blocking(
        switch,
        operation,
        end=end,
        fresh=fresh,
        assert_reason=cause,
    )
    for _ in range(20):
        switch.drive(admitted.operation_id)
        if "runtime-install" in switch.executor.events:
            break
    assert switch.executor.events.count("runtime-install") == 1
    with switch.sessions() as session:
        assert session.get(Job, admitted.operation_id) is not None
        assert len(list(session.scalars(select(RecipeInstallation)))) == 1


def test_cancellation_during_expiring_compile_prevents_a_subsequent_attempt(
    tmp_path: Path,
) -> None:
    """Cancellation on the expiry path prevents another compilation attempt.

    The cancellation lands while the Controller is compiling and the host
    changes, so the expired preflight hold is the first code to see it.  It
    has to finish the cancellation under the same checkpoint guard the
    executing path uses instead of queueing another probe or accepting the
    identical plan later.
    This does not exercise cancellation racing a successful acceptance.
    """

    switch = _cold_compile_switch(tmp_path, slow_compiles=99)
    service, operation = switch.service, switch.operation
    _host_changes_during_compile(switch)
    switch.hooks.append(
        lambda: service.cancel(
            operation.operation_id,
            actor="admin",
            request_key=str(uuid.uuid4()),
            reason="Operator changed their mind during the cold compile",
        )
    )

    for _ in range(20):
        if service.get(operation.operation_id).state not in {"queued", "running"}:
            break
        switch.drive()

    view = service.get(operation.operation_id)
    assert view.state == "cancelled"
    assert switch.executor.events.count("runtime-plan") == 1
    assert "runtime-install" not in switch.executor.events
    with switch.sessions() as session:
        assert list(session.scalars(select(RecipeInstallation))) == []
        assert list(session.scalars(select(ResourceReservation))) == []


def test_preflight_reuse_ignores_operation_history_ordering(tmp_path: Path) -> None:
    """Catches an old operation timestamp poisoning fresh inventory evidence."""
    from vonk_agent_protocol import AgentOperation as OperationKind

    switch = _cold_compile_switch(tmp_path, seconds=1, slow_compiles=0)
    # One second earlier only tells the old receipts from the fresh ones.
    earlier_boot = switch.clock.now - timedelta(seconds=1)
    record_passing_preflight(switch.sessions, earlier_boot)
    # The old receipts are from an earlier boot: the host identity is what
    # makes the Controller probe again.
    with switch.sessions.begin() as session:
        for node in session.scalars(select(AgentNode)):
            set_inventory_fingerprint(node, "b" * 64)
    with switch.sessions() as session:
        stale = {
            operation.id: (operation.node_id, operation.payload_digest)
            for operation in session.scalars(
                select(AgentOperation).where(
                    AgentOperation.kind == OperationKind.RUNTIME_PREFLIGHT.value,
                    AgentOperation.created_at == earlier_boot,
                )
            )
        }
    assert stale
    with switch.sessions() as session:
        baseline = tuple(
            session.scalars(
                select(AgentOperation.id).where(
                    AgentOperation.kind == OperationKind.RUNTIME_PREFLIGHT.value
                )
            )
        )

    def reorder_history() -> None:
        with switch.sessions.begin() as session:
            for operation_id in stale:
                operation = session.get(AgentOperation, operation_id)
                assert operation is not None
                operation.updated_at = switch.clock.now + timedelta(seconds=1)

    switch.hooks.append(reorder_history)
    for _ in range(40):
        switch.drive()
        if "runtime-install" in switch.executor.events:
            break
    assert switch.executor.events.count("runtime-install") == 1
    with switch.sessions() as session:
        fresh = list(
            session.scalars(
                select(AgentOperation).where(
                    AgentOperation.kind == OperationKind.RUNTIME_PREFLIGHT.value,
                    AgentOperation.id.not_in(baseline),
                )
            )
        )
        identities = [
            (operation.node_id, operation.payload_digest) for operation in fresh
        ]
        assert len(identities) == len(set(identities))
        for node_id, digest in stale.values():
            selected = latest_result(session, node_id, requirements_sha256=digest)
            assert selected is not None
            assert selected.result.observed_at > int(earlier_boot.timestamp())


@pytest.mark.usefixtures("damaged_json_rows")
def test_uncached_build_receipt_reaches_copy_after_restart_without_replay(
    tmp_path: Path,
) -> None:
    sessions, lifecycle, _queue, _mapping_id, build_id, nodes = setup_services(tmp_path)
    with sessions.begin() as session:
        build = session.get(RecipeBuild, build_id)
        assert build is not None
        build.state = "planned"
        build.image_digest = None
        build.oci_layout_sha256 = None
        build.image_bytes = None
        revision = session.get(CatalogDocumentRevision, build.recipe_revision_id)
        assert revision is not None
        # Keep the complete persisted build envelope.  A restart consumes the
        # durable executable plan; an identity-only fixture is malformed DB
        # state and must fail closed.
        session.add(
            RecipeSourceBundle(
                sha256=build.source_bundle_sha256,
                media_type="application/vnd.vonk-forge.source-bundle.v1+tar",
                archive_bytes=1,
                total_bytes=1,
                file_count=1,
                storage_key="source-bundle-uncached-build",
                manifest={"schema_version": 1},
                verified_at=NOW,
            )
        )
        node = session.get(AgentNode, nodes[0])
        assert node is not None
        set_inventory_identity(node, binary_digest="a" * 64)
        snapshot = session.scalar(
            select(NodeInventorySnapshot).where(
                NodeInventorySnapshot.node_id == nodes[0]
            )
        )
        assert snapshot is not None
        snapshot.capabilities = ["recipe.build.v1", "recipe.image.pull.v1"]
        build_plan = RecipeBuildPlan(
            build_id=build.id,
            recipe_revision_id=revision.id,
            recipe_content_sha256=revision.content_digest,
            builder_node_id=nodes[0],
            source_bundle_sha256=build.source_bundle_sha256,
            build_input_sha256=build.build_input_sha256,
            agent_payload={"platform": "linux/arm64"},
        )

    child_id = str(uuid.uuid4())
    build_preview_calls: list[str] = []
    build_start_calls: list[str] = []
    build_start_plans: list[RecipeBuildPlan] = []

    def preview_build(recipe_revision_id, builder_node_id):
        del recipe_revision_id
        build_preview_calls.append(builder_node_id)
        return replace(
            build_plan,
            build_id=str(uuid.uuid4()),
            build_input_sha256="a" * 64,
        )

    def start_build(plan, **_kwargs) -> RecipeOperationView:
        build_start_calls.append("start")
        build_start_plans.append(plan)
        return RecipeOperationView(
            id=child_id,
            kind="recipe.build.v1",
            owner_id=build_id,
            state="building",
            plan_digest=plan.build_input_sha256,
            nodes=(),
            lifecycle_result=None,
        )

    lifecycle.preview_build = preview_build
    lifecycle.build = start_build
    executor = BuildThenCopyExecutor(lifecycle, sessions)
    service = RunSwitchOperationService(
        sessions,
        lifecycle=lifecycle,
        clock=lambda: NOW,
        artifacts=CompleteArtifactInspector(),
        phase_executor=executor,
        artifact_phase_executor=executor,
        memory_floor_bytes=50,
    )
    request = _request(sessions, nodes[0])
    plan = service.preview(request, actor="admin")
    assert plan.allowed, [reason.code for reason in plan.blockers]
    assert plan.build.state == "planned"
    assert plan.build.image_digest is None
    assert [(phase.kind, phase.subphase) for phase in plan.phases[:4]] == [
        ("prepare", "container-build"),
        ("prepare", "runtime-image"),
        ("prepare", "runtime-plan"),
        ("transfer", "target-copy"),
    ]
    second_plan = service.preview(request, actor="admin")
    assert second_plan.allowed, [reason.code for reason in second_plan.blockers]
    assert second_plan.plan_digest == plan.plan_digest

    operation = service.apply(
        RunSwitchApplyRequest(
            **request.model_dump(),
            plan_digest=plan.plan_digest,
            request_key=str(uuid.uuid4()),
        ),
        actor="admin",
    )
    assert service.tick() is True
    waiting = service.get(operation.operation_id)
    assert waiting.current_phase == "prepare"
    assert waiting.progress.subphase == "container-build"
    assert waiting.result is not None
    assert waiting.result.child_operation_id == child_id
    # Apply consumes the plan persisted during preview.  A fresh planner call
    # would admit mutable builder evidence and can derive a new identity.
    assert build_preview_calls == []
    assert build_start_calls == ["start"]
    assert build_start_plans[0].build_id == build_id
    assert build_start_plans[0].build_input_sha256 == build_plan.build_input_sha256
    assert (
        build_start_plans[0].agent_payload["recipe_content_sha256"]
        == revision.content_digest
    )

    restarted = RunSwitchOperationService(
        sessions,
        lifecycle=lifecycle,
        clock=lambda: NOW,
        artifacts=CompleteArtifactInspector(),
        phase_executor=executor,
        artifact_phase_executor=executor,
        memory_floor_bytes=50,
    )
    # A restart records the immutable observation deadline once.
    assert restarted.tick() is True
    assert restarted.tick() is False
    assert build_preview_calls == []
    assert build_start_calls == ["start"]

    _replace_child(executor, child_id, state="succeeded")
    with sessions.begin() as session:
        completed = session.get(RecipeBuild, build_id)
        assert completed is not None
        completed.state = "succeeded"
        completed.image_digest = "sha256:" + "1" * 64
        completed.oci_layout_sha256 = "3" * 64
        completed.image_bytes = 30
    assert restarted.tick() is True
    resumed = restarted.get(operation.operation_id)
    assert resumed.current_phase == "prepare"
    assert resumed.progress.subphase == "runtime-image"
    assert resumed.result is not None
    receipt = effective_build_receipt(plan, resumed.result)
    assert receipt is not None
    assert receipt.model_dump(mode="json") == {
        "build_id": build_id,
        "build_input_sha256": build_plan.build_input_sha256,
        "image_digest": "sha256:" + "1" * 64,
        "oci_layout_sha256": "3" * 64,
        "image_bytes": 30,
    }
    assert restarted.tick() is True
    assert restarted.get(operation.operation_id).progress.subphase == "runtime-plan"
    assert restarted.tick() is True
    assert restarted.get(operation.operation_id).current_phase == "transfer"
    assert restarted.get(operation.operation_id).progress.subphase == "target-copy"
    assert restarted.tick() is True
    assert executor.receipts == [receipt]


@pytest.mark.usefixtures("damaged_json_rows")
def test_first_profile_preparation_preview_replans_a_missing_build_archive(
    tmp_path: Path,
) -> None:
    sessions, lifecycle, _queue, _mapping_id, build_id, nodes = setup_services(tmp_path)
    with sessions.begin() as session:
        build = session.get(RecipeBuild, build_id)
        assert build is not None
        revision = session.get(CatalogDocumentRevision, build.recipe_revision_id)
        assert revision is not None
        node = session.get(AgentNode, nodes[0])
        assert node is not None
        set_inventory_identity(node, binary_digest="a" * 64)
        snapshot = session.scalar(
            select(NodeInventorySnapshot).where(
                NodeInventorySnapshot.node_id == nodes[0]
            )
        )
        assert snapshot is not None
        snapshot.capabilities = [*snapshot.capabilities, "recipe.build.v1"]
        if session.get(RecipeSourceBundle, build.source_bundle_sha256) is None:
            session.add(
                RecipeSourceBundle(
                    sha256=build.source_bundle_sha256,
                    media_type="application/vnd.vonk-forge.source-bundle.v1+tar",
                    archive_bytes=1,
                    total_bytes=1,
                    file_count=1,
                    storage_key="restored-source-bundle",
                    manifest={"schema_version": 1},
                    verified_at=NOW,
                )
            )
        build_plan = RecipeBuildPlan(
            build_id=build.id,
            recipe_revision_id=revision.id,
            recipe_content_sha256=revision.content_digest,
            builder_node_id=build.builder_node_id,
            source_bundle_sha256=build.source_bundle_sha256,
            build_input_sha256=build.build_input_sha256,
            agent_payload=dict(build.plan),
            policy_report=dict(build.policy_report),
        )

    def preview_build(recipe_revision_id: str, builder_node_id: str) -> RecipeBuildPlan:
        # RecipeBuildService persists through its own short transaction while
        # Run/Switch still holds the outer profile-preparation Session.
        with sessions.begin() as session:
            build = session.get(RecipeBuild, build_id)
            assert build is not None
            build.state = "planned"
            build.image_digest = None
            build.oci_layout_sha256 = None
            build.image_bytes = None
            build.updated_at = NOW + timedelta(seconds=1)
        return build_plan

    lifecycle.preview_build = preview_build
    service = RunSwitchOperationService(
        sessions,
        lifecycle=lifecycle,
        clock=lambda: NOW,
        artifacts=CompleteArtifactInspector(),
        artifact_phase_executor=RecordingArtifactExecutor(),
        build_archive_available=lambda _digest, _size: False,
        memory_floor_bytes=50,
    )
    request = _request(sessions, nodes[0])
    with sessions() as session:
        before = [(row.id, row.state) for row in session.scalars(select(RecipeBuild))]
        jobs = tuple(session.scalars(select(Job.id)))
    inspected = service.inspect_candidate(
        request.recipe_revision_id, (nodes[0],), actor="viewer"
    )
    assert not inspected.allowed
    with sessions() as session:
        assert [
            (row.id, row.state) for row in session.scalars(select(RecipeBuild))
        ] == before
        assert tuple(session.scalars(select(Job.id))) == jobs
    # The identical normal planning path below really invokes the writer.
    plan = service.preview(_request(sessions, nodes[0]), actor="admin")

    assert plan.allowed, [reason.code for reason in plan.blockers]
    assert plan.installation_id is None
    assert plan.build.build_id == build_id
    assert plan.build.state == "planned"
    assert [(phase.kind, phase.subphase) for phase in plan.phases[:3]] == [
        ("prepare", "container-build"),
        ("prepare", "runtime-image"),
        ("prepare", "runtime-plan"),
    ]


@pytest.mark.usefixtures("damaged_json_rows")
def test_editorial_successor_reuses_an_identity_matched_build(tmp_path: Path) -> None:
    """A notes-only successor reuses the identical prepared build.

    ``persist_plan_in_session`` deliberately reuses a succeeded receipt across a
    revision boundary when the executable input identity matches, keeping the
    older row and its revision id.  The build selection used to reject that row
    because its recorded revision id no longer matched the head, so an editorial
    rename planned a full rebuild of identical bytes.
    """

    sessions, lifecycle, _queue, _mapping_id, build_id, nodes = setup_services(tmp_path)
    with sessions.begin() as session:
        build = session.get(RecipeBuild, build_id)
        assert build is not None
        revision = session.get(CatalogDocumentRevision, build.recipe_revision_id)
        assert revision is not None
        node = session.get(AgentNode, nodes[0])
        assert node is not None
        set_inventory_identity(node, binary_digest="a" * 64)
        snapshot = session.scalar(
            select(NodeInventorySnapshot).where(
                NodeInventorySnapshot.node_id == nodes[0]
            )
        )
        assert snapshot is not None
        snapshot.capabilities = [*snapshot.capabilities, "recipe.build.v1"]
        if session.get(RecipeSourceBundle, build.source_bundle_sha256) is None:
            session.add(
                RecipeSourceBundle(
                    sha256=build.source_bundle_sha256,
                    media_type="application/vnd.vonk-forge.source-bundle.v1+tar",
                    archive_bytes=1,
                    total_bytes=1,
                    file_count=1,
                    storage_key="editorial-source-bundle",
                    manifest={"schema_version": 1},
                    verified_at=NOW,
                )
            )
        document = copy.deepcopy(revision.document)
        reword(document, "Editorially renamed recipe")
        canonical = RecipeDefinition.model_validate(document)
        successor = CatalogDocumentRevision(
            id=str(uuid.uuid4()),
            document_id=revision.document_id,
            kind=revision.kind,
            publisher=revision.publisher,
            slug=revision.slug,
            revision_number=revision.revision_number + 1,
            schema_version=2,
            state="active",
            document=canonical.model_dump(mode="json"),
            content_digest=document_sha256(canonical.model_dump(mode="json")),
            artifact_key="b" * 64,
            execution_key="c" * 64,
            source_bundle_sha256=revision.source_bundle_sha256,
            projected=copy.deepcopy(revision.projected),
            created_by="test",
            created_at=NOW,
        )
        session.add(successor)
        session.flush()
        successor_id = successor.id
        model_digest = canonical.models[0].model.content_sha256
        reused_plan = RecipeBuildPlan(
            build_id=build.id,
            recipe_revision_id=successor_id,
            recipe_content_sha256=document_sha256(canonical.model_dump(mode="json")),
            builder_node_id=build.builder_node_id,
            source_bundle_sha256=build.source_bundle_sha256,
            build_input_sha256=build.build_input_sha256,
            agent_payload=dict(build.plan),
            policy_report=dict(build.policy_report),
        )

    def preview_build(recipe_revision_id: str, builder_node_id: str) -> RecipeBuildPlan:
        del builder_node_id
        assert recipe_revision_id == successor_id
        return reused_plan

    lifecycle.preview_build = preview_build
    service = RunSwitchOperationService(
        sessions,
        lifecycle=lifecycle,
        clock=lambda: NOW,
        artifacts=CompleteArtifactInspector(),
        artifact_phase_executor=RecordingArtifactExecutor(),
        memory_floor_bytes=50,
    )
    plan = service.preview(
        RunSwitchPreviewRequest(
            model_content_sha256=model_digest,
            recipe_revision_id=successor_id,
            spark_group=SparkGroup(
                nodes=[
                    SparkGroupNode(
                        node_id=nodes[0],
                        rank=0,
                        role="entrypoint",
                        endpoint_owner=True,
                    )
                ]
            ),
            alias="qwen",
            action="run",
        ),
        actor="admin",
    )

    assert plan.allowed, [reason.code for reason in plan.blockers]
    assert plan.build is not None
    assert plan.build.build_id == build_id
    assert plan.build.state == "available"
    assert not any(
        phase.kind == "prepare" and phase.subphase == "container-build"
        for phase in plan.phases
    )

    # The successor's own mapping is admitted against the reused build: the
    # build keeps its original owner and the successor uses it by content.
    mappings = ClusterMappingService(sessions)
    successor_mapping = mappings.materialize(
        mappings.preview(successor_id, nodes, {}, "admin"), actor="admin", now=NOW
    )
    admitted = lifecycle._install_admission.plan_install(
        successor_mapping, build_id, now=NOW
    )
    assert admitted.allowed

    # The container-build receipt path and the build's consumer lock take the
    # reused build for the successor plan: an image is its content, not the
    # revision that first built it.
    with sessions.begin() as session:
        receipt = _build_receipt_in_session(session, plan)
        reused = session.get(RecipeBuild, build_id)
        assert reused is not None
        assert reused.recipe_revision_id != successor_id
        assert (
            lock_build_dependency(
                session,
                recipe_revision_id=successor_id,
                builder_node_id=reused.builder_node_id,
                build_input_sha256=reused.build_input_sha256,
                build_id=reused.id,
            )
            is reused
        )
    assert receipt.build_id == build_id
    assert receipt.state == "succeeded"


def test_present_rebuilt_image_replaces_installation_bound_to_missing_build(
    tmp_path: Path,
) -> None:
    sessions, lifecycle, _queue, mapping_id, old_build_id, nodes = setup_services(
        tmp_path
    )
    installed_recipe(
        lifecycle,
        mapping_id,
        old_build_id,
        nodes,
        request_id=str(uuid.uuid4()),
    )
    new_build_id = str(uuid.uuid4())
    new_layout = "5" * 64
    with sessions.begin() as session:
        old_build = session.get(RecipeBuild, old_build_id)
        assert old_build is not None
        installation = session.scalar(
            select(RecipeInstallation).where(
                RecipeInstallation.recipe_build_id == old_build_id
            )
        )
        assert installation is not None
        new_plan = dict(old_build.plan)
        new_plan["build_id"] = new_build_id
        new_plan["build_input_sha256"] = "6" * 64
        session.add(
            RecipeBuild(
                id=new_build_id,
                recipe_revision_id=old_build.recipe_revision_id,
                builder_node_id=old_build.builder_node_id,
                source_bundle_sha256=old_build.source_bundle_sha256,
                build_input_sha256="6" * 64,
                state="succeeded",
                policy_report=dict(old_build.policy_report),
                plan=new_plan,
                image_digest="sha256:" + "2" * 64,
                oci_layout_sha256=new_layout,
                image_bytes=30,
                created_at=NOW + timedelta(seconds=1),
                updated_at=NOW + timedelta(seconds=1),
            )
        )

    service = RunSwitchOperationService(
        sessions,
        lifecycle=lifecycle,
        clock=lambda: NOW,
        artifacts=CompleteArtifactInspector(),
        artifact_phase_executor=RecordingArtifactExecutor(),
        build_archive_available=lambda digest, _size: digest == new_layout,
        memory_floor_bytes=50,
    )
    plan = service.preview(_request(sessions, nodes[0]), actor="admin")

    assert plan.allowed, [reason.code for reason in plan.blockers]
    assert plan.build.build_id == new_build_id
    assert plan.installation_id is None
    assert ("prepare", "runtime-plan") in {
        (phase.kind, phase.subphase) for phase in plan.phases
    }


def test_installed_recipe_starts_after_its_nas_image_expired(tmp_path: Path) -> None:
    """The installed image decides; the NAS archive and build row are provenance."""

    sessions, lifecycle, _queue, mapping_id, build_id, nodes = setup_services(tmp_path)
    installed_recipe(
        lifecycle, mapping_id, build_id, nodes, request_id=str(uuid.uuid4())
    )

    plan = _installed_service(sessions, lifecycle, archive_present=False).preview(
        _request(sessions, nodes[0]), actor="admin"
    )

    assert plan.allowed, [reason.code for reason in plan.blockers]
    assert plan.installation_id is not None
    assert plan.installation_state == InstallationState.INSTALLED
    assert _phase_kinds(plan) == ["start", "final_verify"]
    assert plan.build.state != "planned"
    with sessions.begin() as session:
        assert session.scalar(select(func.count()).select_from(RecipeBuild)) == 1
