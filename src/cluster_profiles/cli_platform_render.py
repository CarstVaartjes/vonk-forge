"""Presentation of the generated current platform observation contract."""

from __future__ import annotations

from datetime import datetime

from cluster_profiles.cli_render.common import _field, _reported, _table, _utc
from cluster_profiles.generated_control.models.platform_observation import (
    PlatformObservation,
)


def render_platform(observation: PlatformObservation) -> None:
    _field("Observed", _utc(observation.observed_at))
    build = observation.api.source_sha
    _reported("Controller build", build[:12] if isinstance(build, str) else None)
    # A reused pull request build keeps its commit; the tree is the release's.
    tree = observation.api_source_tree
    _reported("Controller build tree", tree if isinstance(tree, str) else None)
    _field("API contract", observation.api.control_contract_sha256)
    if observation.workers is None:
        _field("Workers", None)
    else:
        _table(
            ("PROCESS", "SOURCE", "CONTRACT", "LOOP", "COMPLETED"),
            [
                (
                    worker.process_instance_id,
                    worker.source_sha,
                    worker.worker_contract_sha256,
                    worker.loop_sequence,
                    _utc(worker.completed_at),
                )
                for worker in observation.workers
            ],
        )
    for worker in observation.workers or []:
        step = worker.running_step
        started = worker.step_started_at
        if isinstance(step, str) and isinstance(started, datetime):
            print(
                f"Worker {worker.process_instance_id[:12]}: "
                f"step {step} running since {_utc(started)}"
            )
    if observation.worker_issue is not None:
        _field("Worker observation", observation.worker_issue)
    stalled = observation.stalled_applications
    if isinstance(stalled, list) and stalled:
        # Informative: these loads repeat the same wait with no progress.
        print("STALLED profile loads:")
        _table(
            ("LOAD", "STATE", "REPEATS", "SINCE", "WAIT"),
            [
                (
                    item.application_id,
                    item.state,
                    item.repeats,
                    _utc(item.first_seen_at),
                    item.detail,
                )
                for item in stalled
            ],
        )
