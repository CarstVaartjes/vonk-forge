"""A model is one set (digest plus bytes); its file count never reaches the plan."""

import uuid

from vonk_control.model_cache import ArtifactSetManifest, ArtifactSpec
from vonk_control.models import Job
from vonk_control.run_switch_contract import (
    ArtifactStorageImpact,
    RunSwitchApplyRequest,
    RunSwitchCachedTransferResult,
)
from vonk_control.run_switch_operations import DatabaseRunSwitchArtifactInspector
from vonk_control.run_switch_operations.interfaces import ArtifactInspection
from vonk_control.run_switch_operations.phase_planning import PhasePlanningMixin
from vonk_control.run_switch_operations.planning_helpers import _run_switch_payload
from vonk_control.strict_json import read_stored_model

from .recipe_operations_support import setup_services
from .test_run_switch_operations import (
    NOW,
    ModelCacheManifestProvider,
    RecordingArtifactExecutor,
    _request,
    _service,
)

FILES = 300  # one production model had 286 files and failed planning at 256
SET_DIGEST = "a" * 64


def _digests() -> list[str]:
    return [f"{index + 1:064x}" for index in range(FILES)]


def _inspection() -> ArtifactInspection:
    return ArtifactInspection(
        required_bytes=FILES,
        reused_bytes=0,
        copied_bytes=0,
        missing_nas_bytes=0,
        missing_spark_bytes=0,
        nas_coverage="complete",
        spark_coverage="complete",
        artifact_set_sha256=SET_DIGEST,
        artifact_set_bytes=FILES,
        artifact_count=FILES,
    )


def test_a_model_of_hundreds_of_files_plans_as_one_set() -> None:
    impact = PhasePlanningMixin._storage(_inspection())
    assert impact.artifact_set_sha256 == SET_DIGEST
    assert impact.artifact_set_bytes == FILES
    assert ArtifactStorageImpact.model_validate(impact.model_dump(mode="json"))


def test_a_new_plan_carries_the_set_not_its_files() -> None:
    """The retired lists stay readable for stored jobs; a new plan leaves them empty."""
    impact = PhasePlanningMixin._storage(_inspection())
    assert "artifact_digests" not in impact.model_dump(mode="json")
    # A stored plan with the retired fields (file lists, cleanup bytes, retention)
    # still reads: the tolerant stored reader drops them.
    old = {
        **impact.model_dump(mode="json"),
        "artifact_digests": _digests(),
        "reclaimable_digests": [],
        "reclaimable_bytes": 0,
        "reclaimed_bytes": 0,
        "retention": "retain-cached",
    }
    assert read_stored_model(ArtifactStorageImpact, old) == impact
    assert RunSwitchCachedTransferResult.model_fields[
        "verified_digests"
    ].default_factory


class _ManyFileModel(ModelCacheManifestProvider):
    """A model whose set holds more files than the retired 256-digest cap."""

    def resolve_artifact_set(self, **kwargs):
        model_digest = str(kwargs["model_content_sha256"])
        return ArtifactSetManifest(
            model_content_sha256=model_digest,
            recipe_revision_sha256="b" * 64,
            model_content_digests=(model_digest,),
            artifacts=tuple(
                ArtifactSpec(
                    key=f"primary:shard-{index}",
                    artifact_id=f"shard-{index}",
                    path=f"shard-{index:04d}.safetensors",
                    kind="huggingface",
                    repository="vonk-forge/primary",
                    source=f"https://huggingface.co/vonk-forge/primary/resolve/main/shard-{index:04d}.safetensors",
                    revision="0" * 40,
                    sha256=f"{index + 1:064x}",
                    expected_bytes=1024,
                    roles=("weights",),
                    model_content_sha256=model_digest,
                )
                for index in range(FILES)
            ),
        )


def test_a_many_file_model_plans_persists_and_reads_back_through_the_real_path(
    tmp_path,
) -> None:
    """Inspector -> planner -> persisted job payload -> strict read (286 files failed)."""
    inspector = DatabaseRunSwitchArtifactInspector()
    inspector.bind_model_cache(_ManyFileModel(missing_nas_bytes=0))
    sessions, lifecycle, _queue, _mapping, _build, nodes = setup_services(tmp_path)
    service = _service(
        sessions,
        NOW,
        lifecycle,
        RecordingArtifactExecutor(),
        artifacts=inspector,
    )
    request = _request(sessions, nodes[0])
    plan = service.preview(request, actor="operator")
    assert plan.storage.artifact_set_bytes == FILES * 1024
    operation = service.apply(
        RunSwitchApplyRequest(
            **request.model_dump(),
            request_key=str(uuid.uuid4()),
            plan_digest=plan.plan_digest,
        ),
        actor="operator",
    )
    with sessions() as session:
        job = session.get(Job, operation.operation_id)
        assert job is not None
        stored = _run_switch_payload(job)
    assert stored is not None
    assert stored.plan.storage.artifact_set_sha256 == plan.storage.artifact_set_sha256


def test_a_set_of_more_than_a_thousand_files_is_a_valid_manifest() -> None:
    """The manifest byte limit bounds a set; a file count never did the right thing."""
    from vonk_control.model_cache import ArtifactSetManifest, ArtifactSpec
    from vonk_control.model_cache.artifacts import _validate_manifest

    model = "e" * 64
    manifest = ArtifactSetManifest(
        model_content_sha256=model,
        recipe_revision_sha256=None,
        model_content_digests=(model,),
        artifacts=tuple(
            ArtifactSpec(
                key=f"primary:shard-{index}",
                artifact_id=f"shard-{index}",
                path=f"shard-{index:05d}.safetensors",
                kind="huggingface",
                repository="vonk-forge/primary",
                source=f"https://huggingface.co/vonk-forge/primary/resolve/main/s{index}",
                revision="0" * 40,
                sha256=f"{index + 1:064x}",
                expected_bytes=1,
                roles=("weights",),
                model_content_sha256=model,
            )
            for index in range(1100)
        ),
    )
    _validate_manifest(manifest)
