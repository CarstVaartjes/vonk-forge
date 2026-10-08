"""Presentation of the generated current platform observation contract."""

from __future__ import annotations

from .generated_control.models.platform_observation import PlatformObservation


def render_platform(observation: PlatformObservation) -> None:
    # Import lazily: the shared terminal helpers live with the task renderer.
    from .cli_render import _field, _table, _time

    _field("Observed", _time(observation.observed_at.isoformat()))
    _field("API source", observation.api.source_sha)
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
                    _time(worker.completed_at.isoformat()),
                )
                for worker in observation.workers
            ],
        )
    if observation.worker_issue is not None:
        _field("Worker observation", observation.worker_issue)
