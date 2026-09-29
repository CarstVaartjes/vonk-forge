from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from vonk_agent_protocol import DistributionObject
from vonk_control.agent_jobs import AgentJobService
from vonk_control.distribution import (
    DistributionService,
    MemoryObjectSource,
)
from vonk_control.distribution_assignment import NodeDistributionAssignment
from vonk_control.distribution_executor import DurableDistributionPhaseExecutor
from vonk_control.models import Base, RuntimeImageAuthorization
from vonk_control.run_switch_contract import RunSwitchPhase, RunSwitchPlan
from vonk_control.runtime_image_preparation import (
    FilesystemRuntimeImageStorage,
    persist_runtime_image_receipt,
)

from .test_runtime_image_preparation import (
    ARCHIVE_DIGEST,
    BUILD_ID,
    BUILT_IMAGE_DIGEST,
    _add_build,
    _add_revision,
    _prepare,
    _recipe,
)


class _ModelObjectSource(MemoryObjectSource):
    """Memory source that also answers the exact model-set lookup."""

    def objects_for_set(
        self, artifact_set_sha256: str
    ) -> tuple[DistributionObject, ...]:
        return self.artifact_manifests[artifact_set_sha256]


def test_built_image_receipt_flows_from_prepare_to_target_verify(
    tmp_path: Path,
) -> None:
    storage = FilesystemRuntimeImageStorage(tmp_path / "objects")
    receipt = _prepare(storage=storage)
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    revision_id = "revision-direct"
    model_set_digest = "a" * 64
    model = DistributionObject(
        name="weights.bin", sha256="b" * 64, bytes=7, kind="model"
    )
    source = _ModelObjectSource()
    source.register_artifact_set(model_set_digest, (model,))
    # This fixture source owns no Controller image cache, so it declares the
    # prepared archive exactly as MemoryObjectSource intends. The real
    # path reads the receipt in the cache root instead.
    source.register_runtime_image(BUILT_IMAGE_DIGEST, ARCHIVE_DIGEST)
    executor = DurableDistributionPhaseExecutor(
        sessions,
        AgentJobService(sessions, clock=lambda: datetime.now(UTC)),
        DistributionService(source, sessions=sessions),
        clock=lambda: datetime.now(UTC),
    )
    with Session(engine) as session:
        _add_revision(session, revision_id, _recipe("recipe-source-build.json"))
        _add_build(session, revision_id)
        persist_runtime_image_receipt(
            session,
            recipe_revision_id=revision_id,
            original_content_digest=receipt.distribution_content_sha256,
            effective_execution_key="f" * 64,
            receipt=receipt,
            verified_at=datetime.now(UTC),
        )
        session.commit()
    nodes = ("spk_" + "1" * 32,)
    plan = RunSwitchPlan.model_construct(
        preparation=None,
        storage=SimpleNamespace(artifact_digests=[model.sha256]),
        image_digest=BUILT_IMAGE_DIGEST,
        build=SimpleNamespace(oci_layout_sha256=None, image_bytes=None),
        recipe_build_id=BUILD_ID,
        recipe_revision_id=revision_id,
        recipe_content_sha256=receipt.distribution_content_sha256,
        generated_at=datetime.now(UTC),
        plan_digest="c" * 64,
        mapping=None,
    )
    runtime_result = {
        "runtime_image": {
            **receipt.to_mapping(),
            "oci_layout_sha256": ARCHIVE_DIGEST,
        },
        "effective_execution_key": "f" * 64,
    }
    runtime_plan_result = {
        "model_artifact_set_sha256": model_set_digest,
        "model_artifact_set_bytes": model.bytes,
    }
    captured: dict[str, NodeDistributionAssignment] = {}

    def ensure_child(*_args: object, **kwargs: object) -> str:
        assignments = kwargs.get("assignments")
        if isinstance(assignments, Mapping):
            captured.update(assignments)
        return "child-direct"

    executor._ensure_child = ensure_child
    phase = RunSwitchPhase(
        index=0,
        kind="transfer",
        state="planned",
        node_ids=list(nodes),
        detail="transfer phase",
    )
    first = executor.execute(
        plan,
        phase,
        item_index=0,
        actor="operator",
        request_key="00000000-0000-4000-8000-000000000001",
        progress={
            "workload_intent_ordinal": 1,
            "phase_results": [runtime_result, runtime_plan_result],
        },
    )
    assert first.operation_id == "child-direct"
    assignment = next(iter(captured.values())).to_mapping()
    assert assignment["oci_image_digest"] == BUILT_IMAGE_DIGEST
    assert assignment["oci_archive_sha256"] == ARCHIVE_DIGEST
    assert assignment["model_artifact_set_sha256"] == model_set_digest
    verify = executor.execute(
        plan,
        RunSwitchPhase(
            index=1,
            kind="verify",
            state="planned",
            node_ids=list(nodes),
            detail="verify phase",
        ),
        item_index=0,
        actor="operator",
        request_key="00000000-0000-4000-8000-000000000001",
        progress={
            "phase_results": [
                runtime_result,
                runtime_plan_result,
                {"assignments": {nodes[0]: assignment}},
            ],
            "evidence": [{"node_id": nodes[0], "downloaded_bytes": model.bytes}],
        },
    )
    assert verify.result is not None
    assert verify.result["verified"] is True
    with Session(engine) as session:
        session.query(RuntimeImageAuthorization).delete(synchronize_session=False)
        session.commit()
    with pytest.raises(RuntimeError, match="not authorized"):
        executor._archive(
            plan,
            build_id=BUILD_ID,
            image_digest=BUILT_IMAGE_DIGEST,
            layout_digest=ARCHIVE_DIGEST,
            image_bytes=receipt.image_bytes,
        )


def _archive_gate_service(tmp_path: Path):
    """A distribution service whose source carries a real image cache."""

    from vonk_control.distribution import (
        DistributionService,
        RecipeBuildObjectSource,
    )

    storage = FilesystemRuntimeImageStorage(tmp_path)
    receipt = _prepare(storage=storage)
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    with Session(engine) as session:
        _add_revision(session, "gate-revision", _recipe("recipe-source-build.json"))
        _add_build(session, "gate-revision")
        persist_runtime_image_receipt(
            session,
            recipe_revision_id="gate-revision",
            original_content_digest=receipt.distribution_content_sha256,
            effective_execution_key="f" * 64,
            receipt=receipt,
            verified_at=datetime.now(UTC),
        )
        session.commit()
    source = RecipeBuildObjectSource(sessions, tmp_path)
    service = DistributionService(source, sessions=sessions)
    plan = RunSwitchPlan.model_construct(
        preparation=None,
        storage=SimpleNamespace(artifact_digests=["b" * 64]),
        image_digest=BUILT_IMAGE_DIGEST,
        build=SimpleNamespace(oci_layout_sha256=None, image_bytes=None),
        recipe_build_id=BUILD_ID,
        recipe_revision_id="gate-revision",
        recipe_content_sha256=receipt.distribution_content_sha256,
        generated_at=datetime.now(UTC),
        plan_digest="c" * 64,
        mapping=None,
    )
    executor = DurableDistributionPhaseExecutor(
        sessions,
        AgentJobService(sessions, clock=lambda: datetime.now(UTC)),
        service,
        clock=lambda: datetime.now(UTC),
    )
    return executor, plan, receipt, sessions


def test_archive_gate_requires_the_authorization_even_when_bytes_are_stored(
    tmp_path: Path,
) -> None:
    """A stored archive with no live authorization is not usable.

    It fails on the wrong implementation that treats managed-storage presence
    as sufficient, which would serve bytes whose authorization was deleted.
    """

    executor, plan, receipt, sessions = _archive_gate_service(tmp_path)
    call = {
        "build_id": BUILD_ID,
        "image_digest": BUILT_IMAGE_DIGEST,
        "layout_digest": ARCHIVE_DIGEST,
        "image_bytes": receipt.image_bytes,
        "effective_execution_key": "f" * 64,
    }
    assert executor._archive(plan, **call).sha256 == ARCHIVE_DIGEST
    with Session(sessions.kw["bind"]) as session:
        session.query(RuntimeImageAuthorization).delete(synchronize_session=False)
        session.commit()
    # The bytes and their receipt are still in managed storage.
    assert (tmp_path / "image-cache" / ARCHIVE_DIGEST).is_file()
    with pytest.raises(RuntimeError, match="not authorized"):
        executor._archive(plan, **call)


def test_archive_gate_requires_the_stored_receipt_even_when_authorized(
    tmp_path: Path,
) -> None:
    """A live authorization with no stored receipt is not usable either.

    It fails on the wrong implementation that trusts the SQL authorization
    alone, which would admit an archive removed from managed storage.
    """

    executor, plan, receipt, _sessions = _archive_gate_service(tmp_path)
    call = {
        "build_id": BUILD_ID,
        "image_digest": BUILT_IMAGE_DIGEST,
        "layout_digest": ARCHIVE_DIGEST,
        "image_bytes": receipt.image_bytes,
        "effective_execution_key": "f" * 64,
    }
    assert executor._archive(plan, **call).sha256 == ARCHIVE_DIGEST
    (tmp_path / "image-cache" / f"{ARCHIVE_DIGEST}.receipt.json").unlink()
    with pytest.raises(RuntimeError, match="receipt authority"):
        executor._archive(plan, **call)
