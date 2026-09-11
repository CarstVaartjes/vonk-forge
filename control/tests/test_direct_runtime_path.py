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
    DistributionAssignment,
    DistributionService,
    MemoryVerifiedObjectSource,
)
from vonk_control.distribution_executor import DurableDistributionPhaseExecutor
from vonk_control.models import Base, RecipeBuild, RuntimeImageReceipt
from vonk_control.run_switch_contract import RunSwitchPhase, RunSwitchPlan
from vonk_control.runtime_image_preparation import (
    FilesystemRuntimeImageStorage,
    persist_runtime_image_receipt,
    prepare_runtime_image,
)

from .test_runtime_image_preparation import (
    ARCHIVE_DIGEST,
    IMAGE_DIGEST,
    PLATFORM_IMAGE_DIGEST,
    TinyTransport,
    _add_revision,
    _recipe,
    _runtime,
)


class _ModelObjectSource(MemoryVerifiedObjectSource):
    """Memory source that also answers the exact model-set lookup."""

    def objects_for_set(self, artifact_set_sha256: str) -> tuple[DistributionObject, ...]:
        return self.artifact_manifests[artifact_set_sha256]


def test_direct_image_receipt_flows_from_prepare_to_target_verify(tmp_path: Path) -> None:
    storage = FilesystemRuntimeImageStorage(tmp_path / "objects")
    receipt = prepare_runtime_image(
        _recipe("recipe-image.json"),
        runtime=_runtime(),
        storage=storage,
        transport=TinyTransport(),
    )
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    revision_id = "revision-direct"
    model_set_digest = "a" * 64
    model = DistributionObject(name="weights.bin", sha256="b" * 64, bytes=7, kind="model")
    source = _ModelObjectSource()
    source.register_artifact_set(model_set_digest, (model,))
    executor = DurableDistributionPhaseExecutor(
        sessions,
        AgentJobService(sessions, clock=lambda: datetime.now(UTC)),
        DistributionService(source, sessions=sessions),
        clock=lambda: datetime.now(UTC),
    )
    with Session(engine) as session:
        _add_revision(session, revision_id, _recipe("recipe-image.json"))
        persist_runtime_image_receipt(
            session,
            recipe_revision_id=revision_id,
            original_content_digest=receipt.distribution_content_sha256,
            effective_execution_key="f" * 64,
            receipt=receipt,
            verified_at=datetime.now(UTC),
        )
        session.commit()
        assert session.query(RecipeBuild).count() == 0
    nodes = ("spk_" + "1" * 32,)
    plan = RunSwitchPlan.model_construct(
        preparation=None,
        storage=SimpleNamespace(artifact_digests=[model.sha256]),
        image_digest=IMAGE_DIGEST,
        build=SimpleNamespace(oci_layout_sha256=None, image_bytes=None),
        recipe_build_id=None,
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
    captured: dict[str, DistributionAssignment] = {}

    def ensure_child(*_args: object, **kwargs: object) -> str:
        assignments = kwargs.get("assignments")
        if isinstance(assignments, Mapping):
            captured.update(assignments)
        return "child-direct"

    executor._ensure_child = ensure_child
    phase = RunSwitchPhase(
        index=0, kind="transfer", state="planned", node_ids=list(nodes), detail="transfer phase"
    )
    first = executor.execute(
        plan,
        phase,
        item_index=0,
        actor="operator",
        request_key="00000000-0000-4000-8000-000000000001",
        progress={"phase_results": [runtime_result, runtime_plan_result]},
    )
    assert first.operation_id == "child-direct"
    assignment = next(iter(captured.values())).to_mapping()
    assert assignment["oci_image_digest"] == PLATFORM_IMAGE_DIGEST
    assert assignment["oci_archive_sha256"] == ARCHIVE_DIGEST
    assert assignment["model_artifact_set_sha256"] == model_set_digest
    verify = executor.execute(
        plan,
        RunSwitchPhase(
            index=1, kind="verify", state="planned", node_ids=list(nodes), detail="verify phase"
        ),
        item_index=0,
        actor="operator",
        request_key="00000000-0000-4000-8000-000000000001",
        progress={
            "phase_results": [runtime_result, runtime_plan_result, {"assignments": {nodes[0]: assignment}}],
            "evidence": [
                {
                    "node_id": nodes[0],
                    "verified": True,
                    "verified_digests": [model.sha256],
                    "verified_image_digest": PLATFORM_IMAGE_DIGEST,
                    "imported_image_digest": PLATFORM_IMAGE_DIGEST,
                    "verified_oci_layout_sha256": ARCHIVE_DIGEST,
                }
            ],
        },
    )
    assert verify.result is not None
    assert verify.result["verified"] is True
    assert plan.recipe_build_id is None
    with Session(engine) as session:
        session.query(RuntimeImageReceipt).delete(synchronize_session=False)
        session.commit()
    with pytest.raises(RuntimeError, match="receipt authority"):
        executor._archive(
            plan,
            build_id=None,
            image_digest=PLATFORM_IMAGE_DIGEST,
            layout_digest=ARCHIVE_DIGEST,
            image_bytes=receipt.image_bytes,
        )
