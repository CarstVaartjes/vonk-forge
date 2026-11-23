from __future__ import annotations

import hashlib

import pytest
from vonk_agent_protocol import (
    AgentResult,
    OperationProgress,
    RecipeJobFile,
    RecipeJobRunResult,
    canonical_message,
    recipe_job_manifest_sha256,
)
from vonk_control import artifact_job_states as ajs
from vonk_control.models import (
    Job,
)

from .artifact_jobs_support import (
    _two_rank_claims,
    cancellation_result,
    submitted_artifact_job,
)


@pytest.mark.parametrize("companion_ends", ["cancelled", "exits-zero", "fails"])
@pytest.mark.parametrize("owner_outcome", ["succeeded", "failed"])
def test_a_running_companion_is_told_to_stop_when_the_owner_ends(
    tmp_path, owner_outcome: str, companion_ends: str
) -> None:
    (
        sessions,
        _recipe_operations,
        service,
        agent_jobs,
        run_id,
        submitted,
        (owner_node, owner),
        (_companion_node, companion),
        _nodes,
    ) = _two_rank_claims(
        tmp_path, 200 + 10 * ["cancelled", "exits-zero", "fails"].index(companion_ends)
    )
    if owner_outcome == "succeeded":
        from vonk_agent_protocol import RecipeJobEvidence, RecipeJobOutputManifest

        content = b"image"
        output = RecipeJobFile(
            name="output.png",
            media_type="image/png",
            size_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
        )
        service.put_output(
            submitted.id,
            node_id=owner_node,
            name=output.name,
            media_type=output.media_type,
            expected_sha256=output.sha256,
            content=content,
        )
        agent_jobs.succeed(
            owner,
            RecipeJobRunResult(
                job_id=submitted.id,
                run_id=run_id,
                exit_code=0,
                output_manifest=RecipeJobOutputManifest(
                    schema_version=1,
                    total_bytes=len(content),
                    files=(output,),
                    manifest_sha256=recipe_job_manifest_sha256((output,)),
                ),
                evidence=RecipeJobEvidence(
                    elapsed_milliseconds=100, peak_memory_bytes=None
                ),
            ),
        )
    else:
        agent_jobs.record_result(
            AgentResult.model_validate_json(
                canonical_message(
                    {
                        "fence": str(owner.fence),
                        "state": "failed",
                        "result": {
                            "kind": "failed",
                            "code": "recipe_job_run_failed",
                            "reason": "rank 0 exited",
                        },
                    }
                )
            )
        )
    assert service.get(submitted.id).state == owner_outcome

    def parent_outcome() -> tuple[str, str | None]:
        with sessions() as session:
            parent = session.get(Job, service.get(submitted.id).operation_id)
            assert parent is not None
            return parent.state, parent.status_reason

    # The companion is still running its rank: it is told to stop, so the
    # Spark is free for the next job.
    directive = agent_jobs.heartbeat(companion, OperationProgress(phase="running"), 30)
    assert directive.cancel_requested is True
    owner_reason = "rank 0 exited" if owner_outcome == "failed" else None
    if companion_ends == "cancelled":
        agent_jobs.record_result(
            cancellation_result(
                companion,
                submitted,
                state="cancelled",
                reason="controller cancellation requested",
            )
        )
    elif companion_ends == "fails":
        agent_jobs.record_result(
            AgentResult.model_validate_json(
                canonical_message(
                    {
                        "fence": str(companion.fence),
                        "state": "failed",
                        "result": {
                            "kind": "failed",
                            "code": "recipe_job_run_failed",
                            "reason": "rank 1 exited",
                        },
                    }
                )
            )
        )
    else:
        from vonk_agent_protocol import RecipeJobEvidence, RecipeJobOutputManifest

        agent_jobs.succeed(
            companion,
            RecipeJobRunResult(
                job_id=submitted.id,
                run_id=run_id,
                exit_code=0,
                output_manifest=RecipeJobOutputManifest(
                    schema_version=1,
                    total_bytes=0,
                    files=(),
                    manifest_sha256=recipe_job_manifest_sha256(()),
                ),
                evidence=RecipeJobEvidence(
                    elapsed_milliseconds=100, peak_memory_bytes=None
                ),
            ),
        )
    # The artifact job owns the outcome: it and its parent keep the owner's,
    # however the companion ends afterwards (cancelled, exit 0, or failing).
    assert service.get(submitted.id).state == owner_outcome
    assert parent_outcome() == (owner_outcome, owner_reason)
    # A fresh job on the same run is admitted.
    fresh = submitted_artifact_job(service, run_id, request_suffix=158)
    assert fresh.id != submitted.id and fresh.state not in ajs.ENDED


def test_a_companion_that_fails_first_makes_the_job_and_parent_failed(tmp_path) -> None:
    (
        sessions,
        _recipe_operations,
        service,
        agent_jobs,
        run_id,
        submitted,
        (_owner_node, owner),
        (_companion_node, companion),
        _nodes,
    ) = _two_rank_claims(tmp_path, 188)
    agent_jobs.record_result(
        AgentResult.model_validate_json(
            canonical_message(
                {
                    "fence": str(companion.fence),
                    "state": "failed",
                    "result": {
                        "kind": "failed",
                        "code": "recipe_job_run_failed",
                        "reason": "rank 1 exited",
                    },
                }
            )
        )
    )
    assert service.get(submitted.id).state == "failed"
    from vonk_agent_protocol import RecipeJobEvidence, RecipeJobOutputManifest

    content = b"image"
    output = RecipeJobFile(
        name="output.png",
        media_type="image/png",
        size_bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
    )
    # The owner's late success cannot turn the failed job or its parent around.
    agent_jobs.succeed(
        owner,
        RecipeJobRunResult(
            job_id=submitted.id,
            run_id=run_id,
            exit_code=0,
            output_manifest=RecipeJobOutputManifest(
                schema_version=1,
                total_bytes=len(content),
                files=(output,),
                manifest_sha256=recipe_job_manifest_sha256((output,)),
            ),
            evidence=RecipeJobEvidence(
                elapsed_milliseconds=100, peak_memory_bytes=None
            ),
        ),
    )
    assert service.get(submitted.id).state == "failed"
    with sessions() as session:
        parent = session.get(Job, service.get(submitted.id).operation_id)
        assert parent is not None
        # One fact: the parent carries the artifact job's own state and reason.
        assert parent.state == "failed"
        assert parent.status_reason == service.get(submitted.id).status_reason
        assert "rank 1 exited" in (parent.status_reason or "")
