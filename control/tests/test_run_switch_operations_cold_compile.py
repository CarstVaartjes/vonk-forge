"""Run/Switch operations: cold compile."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from vonk_agent_protocol.agent_operations import AgentOperation as AgentOperationKind
from vonk_agent_protocol.agent_operations import AgentOperation as OperationKind
from vonk_agent_protocol.wire_model import canonical_message
from vonk_control.cluster_mappings import ClusterMappingService
from vonk_control.execution_plan.service import ControllerExecutionPlanService
from vonk_control.jobs.documents import RunSwitchRunIntent
from vonk_control.models.cache import RecipeBuild
from vonk_control.models.catalog import CatalogDocumentRevision
from vonk_control.models.execution import (
    RecipeInstallation,
    ResourceReservation,
)
from vonk_control.models.fleet import AgentNode
from vonk_control.models.jobs import Job
from vonk_control.models.operations import AgentOperation
from vonk_control.recipe_operations.runtime_specs import (
    compile_runtime_spec,
    resolve_recipe_entities,
    split_option_choices,
)
from vonk_control.run_switch_contract.requests import (
    RunSwitchApplyRequest,
)
from vonk_control.run_switch_operations.executor import RecipeLifecyclePhaseExecutor
from vonk_control.run_switch_operations.planning_helpers import _run_switch_payload
from vonk_control.run_switch_operations.provider import RunSwitchOperationProvider
from vonk_control.run_switch_operations.service import RunSwitchOperationService
from vonk_control.runtime.preflight import latest_result
from vonk_control.runtime_image_preparation.contracts import PulledImageEvidence
from vonk_control.runtime_image_preparation.preparation import prepare_runtime_image
from vonk_control.runtime_image_preparation.storage import FilesystemRuntimeImageStorage

from .non_blocking import assert_ended_without_blocking
from .preflight_fixtures import record_passing_preflight
from .recipe_operations_support import NOW, _CanonicalModelCache, setup_services
from .run_switch_operations_support import (
    ColdStartPhaseExecutor,
    CompleteArtifactInspector,
    _cold_compile_switch,
    _finish,
    _fixture_execution,
    _host_changes_during_compile,
    _request,
    _target_copy_evidence,
)
from .runtime_identity_support import (
    set_inventory_fingerprint,
    set_inventory_identity,
)
from .runtime_image_fixtures import place_test_image
from .test_lifecycle_preflight import _clear


def test_cold_model_and_image_plan_defers_compile_until_both_preparations(
    tmp_path: Path,
) -> None:
    sessions, lifecycle, _queue, _mapping_id, _build_id, nodes = setup_services(
        tmp_path
    )
    inspector = CompleteArtifactInspector(missing_spark_bytes=1024)

    class ColdInspector(CompleteArtifactInspector):
        def inspect(self, *args, **kwargs):
            value = super().inspect(*args, **kwargs)
            return replace(
                value,
                missing_nas_bytes=1024,
                nas_coverage="partial",
            )

    executor = ColdStartPhaseExecutor()
    service = RunSwitchOperationService(
        sessions,
        lifecycle=lifecycle,
        clock=lambda: NOW,
        artifacts=ColdInspector(
            missing_spark_bytes=inspector.missing_spark_bytes,
        ),
        phase_executor=executor,
        artifact_phase_executor=executor,
        memory_floor_bytes=50,
    )
    plan = service.preview(_request(sessions, nodes[0]), actor="admin")
    assert plan.allowed, [reason.code for reason in plan.blockers]
    assert [(phase.kind, phase.subphase) for phase in plan.phases[:5]] == [
        ("transfer", "model-download"),
        ("prepare", "runtime-image"),
        ("prepare", "runtime-plan"),
        ("transfer", "target-copy"),
        ("verify", "target-copy"),
    ]
    assert plan.storage.nas_coverage == "partial"
    assert plan.runtime_storage.spark_coverage == "partial"
    assert plan.preparation is not None

    operation = service.apply(
        RunSwitchApplyRequest(
            **_request(sessions, nodes[0]).model_dump(),
            plan_digest=plan.plan_digest,
            request_key=str(uuid.uuid4()),
        ),
        actor="admin",
    )
    for _ in range(4):
        assert service.tick() is True
    assert executor.events == [
        "model-download",
        "runtime-image",
        "runtime-plan",
        "target-copy",
    ]
    assert service.get(operation.operation_id).current_phase == "verify"


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
                    assert revision is not None
                    assert build is not None
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
    assert completed.result is not None
    assert completed.result.retry_attempt is None
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
    assert completed.result is not None
    assert completed.result.retry_attempt is None
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
    assert recovered.result is not None
    assert recovered.result.retry_attempt is None


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
    assert recovered.blockers == []
    assert recovered.next_attempt_at is None


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
        assert view.result is not None
        assert view.next_attempt_at is not None
        due = view.next_attempt_at
        observed_delays.append(int((due - switch.clock.now).total_seconds()))
        assert max(1, cap * 3 // 4) <= observed_delays[-1] <= min(60, cap * 5 // 4)
        assert view.result.retry_attempt == len(observed_delays) + 1
        switch.clock.now = due


def test_preflight_refresh_after_repeated_cold_compiles_ends_and_admits_fresh(
    tmp_path: Path,
) -> None:
    """A host that changes during every compile ends the refresh; fresh intent runs."""

    switch = _cold_compile_switch(tmp_path, slow_compiles=99)
    _host_changes_during_compile(switch)
    service, operation = switch.service, switch.operation
    with switch.sessions() as session:
        payload = _run_switch_payload(session.get(Job, operation.operation_id))
        assert payload is not None
        assert isinstance(payload.intent, RunSwitchRunIntent)
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
