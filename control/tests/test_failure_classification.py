"""Recovery consumes effects and explicit authority, never diagnostic prose."""

from pathlib import Path

import pytest
from vonk_agent_protocol import (
    LifecycleState,
    SecurityRefusalReason,
)
from vonk_control.categorized_errors import SecurityRefused

from .test_run_switch_background_image import (
    _background_image_switch,
    _drive_until,
    _past_image,
)


@pytest.mark.parametrize(
    "diagnostic",
    [
        "peer disconnected",
        f"{SecurityRefusalReason.HOST_HELPER_AUTHORITY_DENIED}! diagnostic only",
        f"{SecurityRefusalReason.HOST_HELPER_AUTHORITY_DENIED}: peer disconnected",
        f"{SecurityRefusalReason.TUF_SIGNATURE_INVALID}: diagnostic only",
    ],
)
def test_transport_diagnostics_do_not_prevent_recovery(
    tmp_path: Path, diagnostic: str
) -> None:
    """Catches prose becoming authority and stranding the accepted load."""
    inner = RuntimeError(diagnostic)
    outer = ValueError("transport observation unavailable")
    outer.__cause__ = inner
    switch = _background_image_switch(
        tmp_path, failures=[outer], old_receipt="missing-adapter"
    )
    try:
        _drive_until(switch, _past_image)
        assert switch.view().state not in {
            LifecycleState.FAILED,
            LifecycleState.CANCELLED,
        }
        assert _past_image(switch.view())
        assert len(switch.inspections) == 4
        receipt = switch.storage.read_receipt(switch.layout_digest)
        assert receipt.oci_archive_sha256 == switch.layout_digest
        # Reopening the real worker retains the repaired verified content.
        switch.restart()
        assert switch.storage.read_receipt(switch.layout_digest) == receipt
    finally:
        switch.worker.composite.close()


def test_explicit_authority_denial_has_no_start_effect_and_allows_fresh_admission(
    tmp_path: Path,
) -> None:
    from uuid import uuid4

    from sqlalchemy import select
    from vonk_agent_protocol import AgentOperation
    from vonk_control.models import Job
    from vonk_control.run_switch_contract import RunSwitchApplyRequest

    from .test_run_switch_operations import _request

    outer = ValueError("wrapped peer denial")
    outer.__cause__ = SecurityRefused(
        "denied", reason=SecurityRefusalReason.HOST_HELPER_AUTHORITY_DENIED
    )
    switch = _background_image_switch(tmp_path, copy_failures=[outer])
    try:
        _drive_until(switch, lambda view: view.state == LifecycleState.FAILED)
        ended = switch.view()
        assert ended.state == LifecycleState.FAILED
        with switch.sessions() as session:
            assert (
                session.scalar(
                    select(Job.id).where(Job.kind == AgentOperation.RECIPE_START)
                )
                is None
            )
        request = _request(switch.sessions, ended.node_ids[0])
        plan = switch.worker.service.preview(request, actor="admin")
        assert plan.allowed
        fresh = switch.worker.service.apply(
            RunSwitchApplyRequest(
                **request.model_dump(),
                plan_digest=plan.plan_digest,
                request_key=str(uuid4()),
            ),
            actor="admin",
        )
        assert fresh.operation_id != ended.operation_id
        assert fresh.state == LifecycleState.QUEUED
    finally:
        switch.worker.composite.close()
