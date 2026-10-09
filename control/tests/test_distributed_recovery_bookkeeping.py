"""Recovery authority that cannot be proven is retired as residue, never raised."""

from __future__ import annotations

import ast
import logging
from pathlib import Path
from typing import Any, cast

import vonk_control.distributed_recovery as recovery_module
from sqlalchemy import func, select
from vonk_agent_protocol import LifecycleState
from vonk_control.distributed_recovery import (
    _accepted_start_authority,
    _decode_phases,
    _enqueue_recovery_stop,
    _RecoveryAuthority,
)
from vonk_control.job_documents import RecipeStopParent
from vonk_control.lifecycle.evidence import Residue
from vonk_control.models import Job, RecipeRun
from vonk_control.strict_json import read_stored_model

from .test_recovery_start_wire import _queued_recovery_restart

#: The only functions that may still raise: they enforce a retained recovery
#: marker at the trust boundary that other modules call (recipe operations, route
#: publication, runtime plan authority), and the injected clock's contract.
_BOUNDARY = frozenset({"recovery_start_plan", "enforce_recovery_deadline", "_aware"})


def test_the_recovery_derivation_does_not_raise_a_refusal_for_bookkeeping() -> None:
    trees = [
        ast.parse(path.read_text())
        for path in Path(recovery_module.__file__).parent.glob("*.py")
    ]
    raising: dict[str, int] = {}
    for function in (node for tree in trees for node in ast.walk(tree)):
        if not isinstance(function, ast.FunctionDef):
            continue
        for node in ast.walk(function):
            if (
                isinstance(node, ast.Raise)
                and isinstance(node.exc, ast.Call)
                and getattr(node.exc.func, "id", "") == "DistributedLifecycleError"
            ):
                raising[function.name] = raising.get(function.name, 0) + 1
    assert set(raising) <= _BOUNDARY, (
        "damaged, missing or stale recovery authority is a Residue the tick settles "
        f"on, not a raise: {sorted(set(raising) - _BOUNDARY)}"
    )


def test_a_start_that_is_not_the_exact_accepted_authority_is_retired_with_its_reason(
    tmp_path: Path, caplog
) -> None:
    sessions, _service, started, _restart, nodes, _phases = _queued_recovery_restart(
        tmp_path
    )
    with sessions.begin() as session:
        run = session.get(RecipeRun, started.owner_id)
        original = session.get(Job, started.id)
        assert run is not None and original is not None
        run.run_generation = 1
        original.state = "failed"
        prior_jobs = tuple(session.scalars(select(Job.id)))
        with caplog.at_level(logging.DEBUG):
            outcome = _accepted_start_authority(session, run, "1" * 64, nodes[0])
        assert isinstance(outcome, Residue)
        assert outcome.kind == "distributed-recovery.authority"
        assert outcome.subject == run.id
        assert tuple(session.scalars(select(Job.id))) == prior_jobs
        assert run.run_generation == 1
        assert original.state == LifecycleState.FAILED
    assert any(
        getattr(record, "residue_kind", "") == "distributed-recovery.authority"
        for record in caplog.records
    )


def test_a_repeated_recovery_stop_request_is_answered_with_the_queued_job(
    tmp_path: Path,
) -> None:
    sessions, _service, started, _restart, _nodes, _phases = _queued_recovery_restart(
        tmp_path
    )
    with sessions.begin() as session:
        run = session.get(RecipeRun, started.owner_id)
        stop = session.scalar(
            select(Job).where(
                Job.kind == "recipe.stop",
                Job.payload["owner_id"].as_string() == started.owner_id,
            )
        )
        assert run is not None and stop is not None
        payload = read_stored_model(RecipeStopParent, stop.payload, from_json=True)
        marker = payload.recovery
        assert marker is not None and payload.phases is not None
        start_phases = _decode_phases(marker.start_phases)
        assert start_phases is not None
        stop_phases = tuple(
            tuple((item.node_id, item.payload) for item in group)
            for group in payload.phases
        )
        before = session.scalar(select(func.count()).select_from(Job))
        answer = _enqueue_recovery_stop(
            session,
            cast(Any, None),
            run,
            _RecoveryAuthority(
                stop_phases=stop_phases,
                start_phases=start_phases,
                recipe_content_sha256="a" * 64,
                deadline=marker.deadline,
                workload_intent_ordinal=payload.workload_intent_ordinal,
                failed_rank=marker.failed_rank,
            ),
            failed_rank=marker.failed_rank,
            now=run.updated_at,
        )
        assert isinstance(answer, Job) and answer.id == stop.id
        assert session.scalar(select(func.count()).select_from(Job)) == before


def test_damaged_stored_phases_decode_to_nothing_instead_of_raising() -> None:
    node_payload = {"node_id": "n", "payload": {}}
    assert _decode_phases(cast(Any, [[node_payload]])) is None
    for damaged in (
        None,
        [],
        [[]],
        [[{"node_id": "n"}]],
        [[{"node_id": 1, "payload": {}}]],
        [[{**node_payload, "extra": 1}]],
    ):
        assert _decode_phases(cast(Any, damaged)) is None
