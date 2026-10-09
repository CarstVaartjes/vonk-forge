"""Background runtime-image preparation drives a real Run/Switch to completion.

Production code: ``RunSwitchOperationService`` with its default lifecycle phase
executor (including the runtime-preflight gate) and the worker's
``CompositeDistributionPhaseExecutor`` with asynchronous image preparation,
publishing through ``_persist_run_switch_runtime_image_reference``. Only the
OCI transport and the neighbouring model/target phases are test adapters.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import uuid
from concurrent.futures import wait
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy import select, update
from vonk_agent_protocol import LifecycleState
from vonk_control.distribution_executor import CompositeDistributionPhaseExecutor
from vonk_control.inventory_repository import (
    InventoryRepository,
    InventorySnapshotInput,
)
from vonk_control.models import (
    CatalogDocumentRevision,
    Job,
    NodeArtifact,
    RecipeBuild,
)
from vonk_control.run_switch_contract import (
    RunSwitchApplyRequest,
    SparkGroup,
    SparkGroupNode,
)
from vonk_control.run_switch_operations import (
    PhaseExecution,
    RunSwitchOperationService,
    _lock_phase_owner,
    _phase_request_key,
    _phase_result,
)
from vonk_control.runtime_adapters import resolve_runtime_adapter
from vonk_control.runtime_image_preparation import (
    FilesystemRuntimeImageStorage,
    PulledImageEvidence,
    RuntimeImagePreparationError,
    RuntimeImageReceipt,
    prepare_runtime_image,
)

from .runtime_image_fixtures import place_test_image
from .test_lifecycle_preflight import _finish
from .test_recipe_operations import NOW, _CanonicalModelCache, setup_services
from .test_run_switch_operations import (
    CompleteArtifactInspector,
    _AdvancingClock,
    _request,
    _target_copy_evidence,
)


class _ColdInspector(CompleteArtifactInspector):
    def inspect(self, *args, **kwargs):
        return replace(
            super().inspect(*args, **kwargs),
            missing_nas_bytes=1024,
            nas_coverage="partial",
        )


class _WorkerArtifactExecutor:
    """The worker's composite executor for the image; adapters elsewhere."""

    def __init__(
        self,
        composite: CompositeDistributionPhaseExecutor,
        copy_failures: list[BaseException],
    ) -> None:
        self.composite = composite
        self.copy_failures = copy_failures

    def execute(self, plan, phase, **kwargs) -> PhaseExecution:
        if phase.subphase == "runtime-image":
            return self.composite.execute(plan, phase, **kwargs)
        if phase.subphase == "model-download":
            missing = plan.storage.missing_nas_bytes
            return PhaseExecution(
                result=_phase_result(
                    {
                        "schema_version": 2,
                        "artifact_set_sha256": plan.preparation.model.artifact_set_sha256,
                        "coverage": "complete",
                        "downloaded_bytes": missing,
                        "total_bytes": missing,
                        "progress": {
                            "phase": "model-download",
                            "completed_bytes": missing,
                            "total_bytes": missing,
                            "total_bytes_known": True,
                        },
                    },
                    phase=phase,
                )
            )
        if self.copy_failures:
            raise self.copy_failures.pop(0)
        return PhaseExecution(
            result=_phase_result(_target_copy_evidence(plan, phase), phase=phase)
        )

    def get(self, operation_id: str):
        raise KeyError(operation_id)


def _old_receipt(
    kind: str,
    *,
    storage: FilesystemRuntimeImageStorage,
    image_digest: str,
    layout_digest: str,
    image_bytes: int,
    build_id: str,
) -> dict[str, object]:
    """The two receipt shapes older Controllers left on the NAS."""

    adapter = resolve_runtime_adapter("vllm", {"node_count": 1})
    current = {
        "schema_version": 2,
        "distribution_publisher": "vonk-forge",
        "distribution_slug": "qwen",
        "distribution_content_sha256": "a" * 64,
        "image_digest": image_digest,
        "oci_archive_sha256": layout_digest,
        "image_bytes": image_bytes,
        "local_image_config_id": "sha256:" + "4" * 64,
        "architecture": "linux-arm64",
        "runtime_interface": "vonk.runtime.v1",
        "runtime_interface_label": "v1",
        "archive_path": str(storage.root / layout_digest),
        "recorded_at": NOW.isoformat(),
        "build_id": build_id,
        "runtime_adapter": adapter.adapter_id,
        "runtime_adapter_sha256": adapter.digest,
    }
    RuntimeImageReceipt.model_validate(current)
    if kind == "sibling":
        # A valid receipt another recipe's download left for the same image.
        return {
            **current,
            "distribution_slug": "sibling-recipe",
            "distribution_content_sha256": "c" * 64,
            "build_id": "sibling-build",
            "build_input_sha256": "d" * 64,
        }
    if kind == "other-adapter":
        # The same image bytes, prepared first by another runtime adapter.
        return {
            **current,
            "distribution_publisher": "another-publisher",
            "runtime_adapter": "another-adapter",
            "runtime_adapter_sha256": "e" * 64,
        }
    if kind == "retired-fields":
        # Schema 2 before recipe contract 2.0 dropped these two fields.
        return {
            **current,
            "platform_manifest_digest": image_digest,
            "local_image_reference": None,
        }
    assert kind == "missing-adapter"
    return {
        key: value
        for key, value in current.items()
        if key not in {"runtime_adapter", "runtime_adapter_sha256"}
    }


def _background_image_switch(
    tmp_path: Path,
    *,
    node_count: int = 1,
    inspect_seconds: int = 0,
    failures: list[BaseException] | None = None,
    old_receipt: str | None = None,
    copy_failures: list[BaseException] | None = None,
    engine=None,
):
    sessions, lifecycle, _queue, _mapping, build_id, nodes = setup_services(
        tmp_path, nodes=node_count, engine=engine
    )
    clock = _AdvancingClock(NOW)
    lifecycle._clock = clock
    with sessions.begin() as session:
        session.query(NodeArtifact).delete()
        build = session.get(RecipeBuild, build_id)
        assert build is not None
        image_digest, layout_digest = build.image_digest, build.oci_layout_sha256
        image_bytes = build.image_bytes
    assert image_digest is not None and layout_digest is not None
    assert image_bytes is not None
    storage = FilesystemRuntimeImageStorage(tmp_path / "controller-artifacts")
    archive = b"canonical-runtime-image-archive"[:image_bytes]
    assert hashlib.sha256(archive).hexdigest() == layout_digest
    place_test_image(storage, layout_digest, len(archive))
    if old_receipt is not None:
        # A receipt an older Controller wrote; the current contract rejects it.
        document = _old_receipt(
            old_receipt,
            storage=storage,
            image_digest=image_digest,
            layout_digest=layout_digest,
            image_bytes=image_bytes,
            build_id=build_id,
        )
        (storage.root / f"{layout_digest}.receipt.json").write_text(
            json.dumps(document)
        )
        if old_receipt not in {"sibling", "other-adapter"}:
            with pytest.raises(Exception) as _ending:
                storage.read_receipt(layout_digest)
    pending_failures = [error for error in failures or () for _ in range(3)]
    pending_copy_failures = list(copy_failures or ())
    inspections: list[str] = []

    class Transport:
        def inspect_archive(self, archive_path, **kwargs) -> PulledImageEvidence:
            inspections.append(str(archive_path))
            # Reading a real multi-GiB archive takes minutes of wall time.
            if pending_failures:
                raise pending_failures.pop(0)
            clock.advance(inspect_seconds)
            return PulledImageEvidence(
                manifest_digest=image_digest,
                config_id="sha256:" + "4" * 64,
                local_reference="oci-archive:" + str(archive_path),
                architecture=kwargs["expected_architecture"],
                runtime_interface="v1",
                archive_sha256=kwargs["expected_archive_sha256"],
                archive_bytes=kwargs["expected_archive_bytes"],
            )

    # PostgreSQL row locks order the background publication after the tick
    # that submitted it; SQLite has none, so the test orders them explicitly.
    tick_done = threading.Event()

    def preparer(document, runtime_spec, build, *, before_publish=None):
        assert tick_done.wait(10)
        return prepare_runtime_image(
            document,
            runtime=runtime_spec.runtime,
            storage=storage,
            transport=cast(Any, Transport()),
            build_receipt={
                "state": build.state,
                "build_id": build.id,
                "build_input_sha256": build.build_input_sha256,
                "image_digest": build.image_digest,
                "oci_layout_sha256": build.oci_layout_sha256,
                "image_bytes": build.image_bytes,
            },
            now=clock.now,
            before_publish=before_publish,
        )

    worker = SimpleNamespace()

    def start_worker() -> None:
        """Compose the worker's services; a second call is a worker restart."""
        worker.composite = CompositeDistributionPhaseExecutor(
            sessions,
            lifecycle._agent_jobs,
            None,
            model_cache=_CanonicalModelCache(),
            runtime_image_preparer=preparer,
            async_runtime_image_preparation=True,
            clock=clock,
        )
        worker.service = RunSwitchOperationService(
            sessions,
            lifecycle=lifecycle,
            clock=clock,
            artifacts=_ColdInspector(missing_spark_bytes=1024),
            artifact_phase_executor=_WorkerArtifactExecutor(
                worker.composite, pending_copy_failures
            ),
            memory_floor_bytes=50,
        )

    start_worker()
    request = _group_request(sessions, nodes)
    plan = worker.service.preview(request, actor="admin")
    assert plan.allowed, [reason.code for reason in plan.blockers]
    assert len(plan.spark_group.nodes) == node_count
    assert [(phase.kind, phase.subphase) for phase in plan.phases[:2]] == [
        ("transfer", "model-download"),
        ("prepare", "runtime-image"),
    ]
    operation = worker.service.apply(
        RunSwitchApplyRequest(
            **request.model_dump(),
            plan_digest=plan.plan_digest,
            request_key=str(uuid.uuid4()),
        ),
        actor="admin",
    )

    def view():
        return worker.service.get(operation.operation_id)

    inventory = InventoryRepository(sessions, clock=clock)
    reported = {"at": NOW}

    def drive() -> None:
        """One worker loop: tick when due, answer probes, let the pool run."""
        current = view()
        due = current.result.observation_due_at if current.result else None
        if due is not None and due > clock.now:
            clock.now = due
        for node_id in nodes if clock.now > reported["at"] else ():
            # Agents keep reporting the same capacity while the Controller works.
            inventory.record(
                InventorySnapshotInput(
                    node_id,
                    clock.now,
                    10_000,
                    8_000,
                    10_000,
                    8_000,
                    10_000,
                    8_000,
                    1,
                    False,
                    ("runtime.vonk.v1", "recipe.image.pull.v1", "recipe.operations.v1"),
                    memory_pool="shared",
                )
            )
        reported["at"] = clock.now
        tick_done.clear()
        worker.service.tick()
        tick_done.set()
        current = view()
        checkpoint = current.result.preflight if current.result else None
        if checkpoint is not None and checkpoint.pending_job_id:
            with sessions() as session:
                pending = session.get(Job, checkpoint.pending_job_id)
                if pending is not None and pending.state in {"queued", "running"}:
                    _finish(sessions, checkpoint, clock.now)
        wait(
            [future for future, _ in worker.composite._runtime_image_futures.values()],
            timeout=10,
        )

    def restart() -> None:
        worker.composite.close()
        start_worker()

    return SimpleNamespace(
        sessions=sessions,
        operation_id=operation.operation_id,
        worker=worker,
        view=view,
        drive=drive,
        restart=restart,
        clock=clock,
        inspections=inspections,
        storage=storage,
        layout_digest=layout_digest,
    )


def _group_request(sessions, nodes: tuple[str, ...]):
    single = _request(sessions, nodes[0])
    return single.model_copy(
        update={
            "spark_group": SparkGroup(
                nodes=[
                    SparkGroupNode(
                        node_id=node_id,
                        rank=rank,
                        role="entrypoint" if rank == 0 else "worker",
                        endpoint_owner=rank == 0,
                    )
                    for rank, node_id in enumerate(nodes)
                ]
            )
        }
    )


def _drive_until(switch, stop, loops: int = 40) -> None:
    for _ in range(loops):
        current = switch.view()
        if current.state in {"failed", "cancelled", "succeeded"} or stop(current):
            return
        switch.drive()


def _past_image(view) -> bool:
    return view.progress.subphase not in {"model-download", "runtime-image"}


def _assert_replan_reaches_the_next_phase(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, engine=None
) -> None:
    switch = _background_image_switch(
        tmp_path,
        node_count=2,
        failures=[RuntimeError("skopeo inspect failed")],
        engine=engine,
    )
    try:
        with caplog.at_level(logging.INFO):
            _drive_until(switch, _past_image)
    finally:
        switch.worker.composite.close()
    # The production signature was one of these per ~7 s cycle, forever: the
    # consumed failure left the switch running without a blocker, the next
    # tick resubmitted, and the blocker reappeared.
    preparing = [
        record
        for record in caplog.records
        if "is waiting: run-switch.runtime-image-preparing" in record.getMessage()
    ]
    assert len(preparing) == 2
    view = switch.view()
    assert view.result is not None
    assert view.result.phase_retry_generation == 0
    assert view.progress.subphase == "runtime-plan", (
        view.state,
        view.status_reason,
        len(switch.inspections),
    )
    # One failed and one successful preparation; no hidden repeat loop.
    assert len(switch.inspections) == 4


def test_background_image_after_a_replan_reaches_the_next_phase(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A phase retry generation must not orphan the background publication.

    After a re-plan the phase runs under the derived per-generation request
    key. Before the fix, publication looked the owner up by the raw key, found
    none and raised ``runtime-image-owner-changed``, which the tick swallowed:
    every finished preparation was dropped and a new one started, forever,
    with only "running in the background" to show for it.
    """

    _assert_replan_reaches_the_next_phase(tmp_path, caplog)


def test_background_image_after_a_replan_reaches_the_next_phase_on_postgres(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, postgres_engine
) -> None:
    """The same, with the production database's NOWAIT row locks."""

    _assert_replan_reaches_the_next_phase(tmp_path, caplog, postgres_engine)


def test_background_image_failure_is_logged_and_shown_as_the_retry_reason(
    tmp_path: Path, caplog
) -> None:
    switch = _background_image_switch(
        tmp_path,
        failures=[
            RuntimeImagePreparationError(
                "artifact.reference_busy", "archive is being cleaned up", retryable=True
            )
        ],
    )
    try:
        with caplog.at_level(logging.WARNING):
            _drive_until(switch, lambda view: view.result.retry_reason is not None)
        held = switch.view()
        assert held.result.observation_due_at is not None
        _drive_until(switch, _past_image)
    finally:
        switch.worker.composite.close()
    assert switch.view().progress.subphase == "runtime-plan"
    assert switch.view().result.retry_reason is None


@pytest.mark.parametrize("old_receipt", ["retired-fields", "missing-adapter"])
@pytest.mark.parametrize("node_count", [1, 2])
@pytest.mark.parametrize("replanned", [False, True])
def test_preparation_longer_than_the_preflight_window_replaces_an_old_receipt(
    tmp_path: Path, node_count: int, replanned: bool, old_receipt: str
) -> None:
    """The production shape: a stale receipt and a minutes-long inspection.

    The receipt an older Controller wrote is rejected, so the archive is
    inspected again; that outlives the 300 s runtime preflight window, so the
    gate re-probes before the finished preparation is consumed.
    """

    switch = _background_image_switch(
        tmp_path,
        node_count=node_count,
        inspect_seconds=400,
        old_receipt=old_receipt,
        failures=[RuntimeError("skopeo inspect failed")] if replanned else [],
    )
    try:
        _drive_until(switch, _past_image)
    finally:
        switch.worker.composite.close()
    view = switch.view()
    assert view.progress.subphase == "runtime-plan", (view.state, view.status_reason)
    assert (view.result.phase_retry_generation or 0) == 0
    # The gate re-probed every Spark during the image phase (attempts reset
    # per phase) and the finished preparation was still consumed.
    preflight = view.result.preflight
    assert preflight is not None and preflight.phase_index == 1
    assert len(preflight.attempts) == node_count
    # The old document was treated as absent and replaced by a current one.
    receipt = switch.storage.read_receipt(switch.layout_digest)
    assert receipt.oci_archive_sha256 == switch.layout_digest


@pytest.mark.parametrize("node_count", [1, 2])
@pytest.mark.parametrize("provenance", ["sibling", "other-adapter"])
def test_a_sibling_recipes_receipt_for_the_same_image_does_not_block_the_load(
    tmp_path: Path, node_count: int, provenance: str
) -> None:
    """An image is its content: whoever recorded it first does not matter.

    Another recipe published this image first, so the stored receipt names its
    publisher, slug, build and input. Publishing the image for this recipe's
    load used to fail with "runtime image no longer matches the approved
    recipe" (the hardware sweep's phi-4 installs).
    """

    switch = _background_image_switch(
        tmp_path, node_count=node_count, old_receipt=provenance
    )
    try:
        _drive_until(switch, _past_image)
    finally:
        switch.worker.composite.close()
    view = switch.view()
    assert view.state != "failed", view.status_reason
    assert view.progress.subphase == "runtime-plan", (view.state, view.status_reason)
    # The stored receipt keeps its content; the load answered with its own build.
    stored = switch.storage.read_receipt(switch.layout_digest)
    assert stored.distribution_publisher != "vonk-forge" or (
        stored.distribution_slug == "sibling-recipe"
    )
    intent = view.result.runtime_image_reference_intent
    assert intent is not None and intent.build_id != "sibling-build"


def test_a_replan_after_publication_republishes_the_image_reference(
    tmp_path: Path,
) -> None:
    """A later phase's re-plan must not strand the earlier plan's reference.

    The image phase publishes a reference intent bound to its plan. When the
    target copy then fails and re-plans, the next generation publishes for
    the new plan and replaces that intent instead of failing the load.
    """

    switch = _background_image_switch(
        tmp_path, copy_failures=[RuntimeError("target copy failed")]
    )
    try:
        _drive_until(
            switch,
            lambda view: view.result.phase_retry_generation == 1 and _past_image(view),
        )
    finally:
        switch.worker.composite.close()
    view = switch.view()
    assert view.state != "failed", view.status_reason
    assert view.result.phase_retry_generation == 1
    assert view.progress.subphase == "runtime-plan", view.status_reason
    intent = view.result.runtime_image_reference_intent
    assert intent is not None
    with switch.sessions() as session:
        job = session.get(Job, switch.operation_id)
        assert job is not None
        assert intent.plan_digest == job.payload["plan_digest"]


def test_unreadable_model_revision_recovers_same_request_after_repair(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """An old-format catalog Model is a stored, logged retry reason.

    This is the path a compile failure takes; it never produces the silent
    "running in the background" loop that an unowned publication did.
    """

    switch = _background_image_switch(tmp_path)
    table = cast(Any, CatalogDocumentRevision.__table__)
    with switch.sessions() as session:
        models = [
            (row.id, row.document)
            for row in session.scalars(
                select(CatalogDocumentRevision).where(
                    CatalogDocumentRevision.kind == "model"
                )
            )
        ]
        # Catalog revisions are immutable through the ORM; an older Controller
        # left documents the current Model contract cannot read.
        for revision_id, document in models:
            session.execute(
                update(table)
                .where(table.c.id == revision_id)
                .values(document={"schema_version": 1, "id": document.get("id")})
            )
        session.commit()
    try:
        with caplog.at_level(logging.INFO):
            _drive_until(switch, lambda view: view.result.retry_reason is not None)
        waiting = switch.view()
        assert waiting.result.observation_due_at is not None
        assert waiting.operation_id == switch.operation_id
        with switch.sessions.begin() as session:
            for revision_id, document in models:
                session.execute(
                    update(table)
                    .where(table.c.id == revision_id)
                    .values(document=document)
                )
        switch.restart()
        _drive_until(switch, _past_image)
        assert switch.view().operation_id == waiting.operation_id
        assert (
            switch.storage.read_receipt(switch.layout_digest).oci_archive_sha256
            == switch.layout_digest
        )
    finally:
        switch.worker.composite.close()


def test_waiting_load_resumes_after_a_worker_restart(tmp_path: Path) -> None:
    """A load parked by the old loop resumes on the new worker by itself."""

    switch = _background_image_switch(
        tmp_path, failures=[RuntimeError("skopeo inspect failed")]
    )
    _drive_until(
        switch,
        lambda view: (
            view.result.phase_retry_generation == 1
            and view.state == LifecycleState.OBSERVING
        ),
    )
    assert switch.view().state == LifecycleState.OBSERVING
    switch.restart()
    try:
        _drive_until(switch, _past_image)
    finally:
        switch.worker.composite.close()
    assert switch.view().progress.subphase == "runtime-plan"


def test_phase_owner_is_its_own_or_current_generation_key(tmp_path: Path) -> None:
    """Image publication and container builds find their owner the same way."""

    switch = _background_image_switch(tmp_path)
    switch.worker.composite.close()
    with switch.sessions.begin() as session:
        job = session.get(Job, switch.operation_id)
        assert job is not None
        job.result = {**job.result, "phase_retry_generation": 2}
        request_id = job.request_id
    with switch.sessions.begin() as session:
        for key in (request_id, _phase_request_key(request_id, 1, 0, 2)):
            owner = _lock_phase_owner(session, key, 1, 0)
            assert owner is not None and owner.id == switch.operation_id
        # An earlier generation's or another phase's key owns nothing.
        assert (
            _lock_phase_owner(session, _phase_request_key(request_id, 1, 0, 1), 1, 0)
            is None
        )
        assert (
            _lock_phase_owner(session, _phase_request_key(request_id, 2, 0, 2), 1, 0)
            is None
        )
