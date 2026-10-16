"""A job whose reported outputs break their contract ends failed at once.

The receipt's manifest is final, so a breach is the same on every later look:
the job ends failed with the rule, its bound and the measured value as typed
evidence, never observed and never under a wait reason.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

import pytest
from vonk_agent_protocol import (
    AgentResult,
    AgentResultState,
    FailureCode,
    OutcomeFailed,
    OutcomeKind,
    OutcomeUnknown,
    RecipeJobFile,
    RecipeJobOutputLimits,
    RecipeJobRunResult,
    WaitReason,
    recipe_job_manifest_document,
    recipe_job_manifest_sha256,
)
from vonk_control.agent_jobs import AgentJobService
from vonk_control.artifact_job_evidence import (
    OUTPUT_CONTRACT_RULES,
    OutputContractBreach,
    OutputContractRule,
)

from .runtime_identity_support import claim_agent
from .test_artifact_jobs import (
    MutableClock,
    running_artifact_service,
    submitted_artifact_job,
)
from .test_recipe_operations import NOW

PNG = "image/png"
JSON = "application/json"


def _two_slots(document: dict) -> None:
    """An image slot (1 to 4 files, 1500 bytes) and an optional metadata slot,
    under a 2000-byte contract: every rule can be the first one broken."""

    output = document["interfaces"][0]["output"]
    output["max_total_bytes"] = 2000
    output["slots"][0].update(max_files=4, max_total_bytes=1500)
    output["slots"].append(
        {
            "id": "metadata",
            "label": "Metadata",
            "description": "Generated metadata",
            "media_types": [JSON],
            "extensions": [".json"],
            "min_files": 0,
            "max_files": 1,
            "max_file_bytes": 1024,
            "max_total_bytes": 1024,
        }
    )


@dataclass(frozen=True)
class Case:
    rule: OutputContractRule
    outputs: tuple[tuple[str, str, int], ...]
    breach: dict[str, object]
    limits: dict[str, object] = field(default_factory=dict)


CASES = (
    Case(
        "job_output_slot",
        (("a.txt", "text/plain", 10),),
        {"entry": "a.txt"},
    ),
    Case(
        "job_output_slot_max_file_bytes",
        (("a.png", PNG, 1100),),
        {"limit": 1024, "observed": 1100, "entry": "a.png", "slot": "image"},
    ),
    Case(
        "job_output_slot_files",
        tuple((f"{name}.png", PNG, 10) for name in "abcde"),
        {"limit": 4, "observed": 5, "slot": "image"},
    ),
    Case(
        "job_output_slot_max_total_bytes",
        (("a.png", PNG, 1000), ("b.png", PNG, 1000)),
        {"limit": 1500, "observed": 2000, "slot": "image"},
    ),
    Case(
        "job_output_contract_max_total_bytes",
        (("a.png", PNG, 1000), ("b.json", JSON, 1024)),
        {"limit": 2000, "observed": 2024},
    ),
    Case(
        "job_output_allowed_media_type",
        (("a.png", PNG, 10), ("b.json", JSON, 10)),
        {"entry": "b.json"},
        {"allowed_media_types": [PNG]},
    ),
    Case(
        "job_output_max_files",
        (("a.png", PNG, 10), ("b.png", PNG, 10)),
        {"limit": 1, "observed": 2},
        {"max_files": 1},
    ),
    Case(
        "job_output_max_file_bytes",
        (("a.png", PNG, 500),),
        {"limit": 100, "observed": 500, "entry": "a.png"},
        {"max_file_bytes": 100},
    ),
    Case(
        "job_output_max_total_bytes",
        (("a.png", PNG, 100), ("b.png", PNG, 100)),
        {"limit": 150, "observed": 200},
        {"max_file_bytes": 100, "max_total_bytes": 150},
    ),
)


def _files(outputs: tuple[tuple[str, str, int], ...]) -> tuple[RecipeJobFile, ...]:
    return tuple(
        RecipeJobFile(
            name=name,
            media_type=media_type,
            size_bytes=size,
            sha256=hashlib.sha256(name.encode()).hexdigest(),
        )
        for name, media_type, size in outputs
    )


def _report(claim, job, state: str, outputs) -> AgentResult:
    """The report the agent sends with the receipt of an exited job."""

    files = _files(outputs)
    exit_code = 3 if state == "failed" else 0
    receipt = RecipeJobRunResult.model_validate(
        {
            "job_id": job.id,
            "run_id": job.run_id,
            "exit_code": exit_code,
            "output_manifest": {
                **recipe_job_manifest_document(files),
                "manifest_sha256": recipe_job_manifest_sha256(files),
            },
            "evidence": {"elapsed_milliseconds": 382, "peak_memory_bytes": None},
        }
    )
    if state == "succeeded":
        return AgentResult(
            fence=claim.fence, state=AgentResultState.SUCCEEDED, result=receipt
        )
    if state == "failed":
        return AgentResult(
            fence=claim.fence,
            state=AgentResultState.FAILED,
            result=OutcomeFailed(
                kind=OutcomeKind.FAILED,
                code=FailureCode.RECIPE_JOB_RUN_FAILED,
                reason="job adapter exited unsuccessfully",
                receipt=receipt,
            ),
        )
    # A refused or lost upload leaves the delivery unobserved on the agent.
    return AgentResult(
        fence=claim.fence,
        state=AgentResultState.OBSERVING,
        result=OutcomeUnknown(
            kind=OutcomeKind.UNKNOWN,
            wait_reason=WaitReason.OBSERVATION_UNAVAILABLE,
            reason="job output delivery remains unobserved",
            receipt=receipt,
        ),
    )


def _report_outputs(tmp_path, state: str, outputs, limits: dict[str, object]):
    """Submit one job, claim it, deliver ``outputs`` in ``state``; the job after."""

    sessions, recipe_operations, _queue, service, run_id, node_id = (
        running_artifact_service(tmp_path, recipe_transform=_two_slots)
    )
    agent_jobs = AgentJobService(sessions, clock=MutableClock(NOW))

    def consume(session, operation, attempt, message) -> None:
        service.consume_agent_result(session, operation, attempt, message)
        recipe_operations.consume_agent_result(session, operation, attempt, message)

    agent_jobs.set_result_consumer(consume)
    recipe_operations._agent_jobs = agent_jobs
    output_limits = RecipeJobOutputLimits.model_validate(
        {
            "max_files": 5,
            "max_file_bytes": 1024,
            "max_total_bytes": 2000,
            "allowed_media_types": [JSON, PNG],
            **limits,
        }
    )
    submitted = submitted_artifact_job(
        service, run_id, request_suffix=310, output_limits=output_limits
    )
    claim = claim_agent(agent_jobs, node_id, "serial-0")
    assert claim is not None
    agent_jobs.record_result(_report(claim, submitted, state, outputs))

    def fresh_job_is_claimed() -> bool:
        fresh = submitted_artifact_job(
            service, run_id, request_suffix=312, output_limits=output_limits
        )
        return (
            fresh.state == "queued"
            and claim_agent(agent_jobs, node_id, "serial-0") is not None
        )

    return service.get(submitted.id), fresh_job_is_claimed


def _assert_failed_with(ended, fresh_job_is_claimed, breach, exit_code: int) -> None:
    assert ended.state == "failed"
    assert ended.supported_actions == ()
    evidence = ended.result_evidence
    assert evidence is not None
    assert evidence.output_breach == breach
    assert evidence.exit_code == exit_code
    # A receipted exit: no lapsed lease, no scope in doubt.
    assert evidence.failure_kind is None
    assert evidence.active_scope_may_remain is not True
    assert fresh_job_is_claimed()


def test_every_output_contract_rule_has_a_case() -> None:
    assert sorted(case.rule for case in CASES) == sorted(OUTPUT_CONTRACT_RULES)


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.rule)
def test_a_manifest_that_breaks_a_rule_ends_the_job_failed_at_once(
    tmp_path, case: Case
) -> None:
    """Wrong implementations caught: a breach ended under the wait reason
    ``observation-unavailable`` with no rule; a breach was observed."""

    ended, fresh_job_is_claimed = _report_outputs(
        tmp_path, "unknown", case.outputs, case.limits
    )
    _assert_failed_with(
        ended,
        fresh_job_is_claimed,
        OutputContractBreach(rule=case.rule, **case.breach),
        exit_code=0,
    )


@pytest.mark.parametrize("state", ["succeeded", "unknown", "failed"])
def test_a_breach_ends_the_job_failed_in_every_reported_state(
    tmp_path, state: str
) -> None:
    """Wrong implementation caught: a succeeded receipt over its file-count
    limit was observed instead of ended."""

    ended, fresh_job_is_claimed = _report_outputs(
        tmp_path, state, (("a.png", PNG, 10), ("b.png", PNG, 10)), {"max_files": 1}
    )
    _assert_failed_with(
        ended,
        fresh_job_is_claimed,
        OutputContractBreach(rule="job_output_max_files", limit=1, observed=2),
        exit_code=3 if state == "failed" else 0,
    )


def test_a_success_without_its_required_output_ends_failed(tmp_path) -> None:
    ended, fresh_job_is_claimed = _report_outputs(tmp_path, "succeeded", (), {})
    _assert_failed_with(
        ended,
        fresh_job_is_claimed,
        OutputContractBreach(
            rule="job_output_slot_files", limit=1, observed=0, slot="image"
        ),
        exit_code=0,
    )


@pytest.mark.parametrize("state", ["succeeded", "unknown", "failed"])
def test_an_in_contract_manifest_is_never_recorded_as_a_breach(
    tmp_path, state: str
) -> None:
    ended, _fresh = _report_outputs(tmp_path, state, (("a.png", PNG, 10),), {})
    assert ended.result_evidence is None or ended.result_evidence.output_breach is None
