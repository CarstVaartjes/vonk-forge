"""A profile load waiting on a gated model carries the typed wait, not prose."""

from __future__ import annotations

from datetime import UTC, datetime

from vonk_agent_protocol import ModelAccessReason, OperationProgress
from vonk_control.distribution_executor.preparation import (
    CompositeDistributionPhaseExecutor,
)
from vonk_control.model_cache.views import CacheOperationView
from vonk_control.model_cache_contract import ModelCacheOperationProgress
from vonk_control.operation_blockers import make_blocker
from vonk_control.run_switch_contract import RunSwitchModelDownloadPendingResult
from vonk_control.strict_json import serialize_json_value
from vonk_control.wait_contract import QueueWait

NOW = datetime(2026, 11, 6, 12, tzinfo=UTC)
RETRY = datetime(2026, 11, 6, 13, tzinfo=UTC)


def test_pending_download_carries_the_typed_wait() -> None:
    blocker = make_blocker(
        ModelAccessReason.ACCESS_DENIED,
        "Hugging Face access is required: accept the terms at https://huggingface.co/o/m",
    )
    progress = serialize_json_value(
        ModelCacheOperationProgress(
            phase="downloading",
            completed_artifacts=0,
            total_artifacts=1,
            downloaded_bytes=0,
            expected_bytes=100,
            total_bytes_known=True,
            measurement=OperationProgress(
                phase="downloading",
                completed_bytes=0,
                total_bytes=100,
                total_bytes_known=True,
                completed_items=0,
                total_items=1,
            ),
        )
    )
    view = CacheOperationView(
        id="96c1a078-ffec-413d-83ab-1a82b04cc4c1",
        request_key="7a20fb2b-9abb-467f-8209-b60bcfba8532",
        kind="download",
        state="queued",
        attempt=1,
        model_content_sha256=None,
        artifact_set_sha256="a" * 64,
        plan_digest=None,
        review_digest=None,
        progress=progress,  # type: ignore[arg-type]
        result=None,
        last_error="free text that must not be the reason",
        created_at=NOW.isoformat(),
        updated_at=NOW.isoformat(),
        completed_at=None,
        blockers=(blocker,),
        wait=QueueWait(
            owner={"kind": "model-cache", "id": "96c1a078-ffec-413d-83ab-1a82b04cc4c1"},  # type: ignore[arg-type]
            owner_kind="model-cache",  # type: ignore[arg-type]
            reason=ModelAccessReason.ACCESS_DENIED,
            blockers=[blocker],
            first_blocked_at=NOW,
            last_progress_at=None,
            resume_condition="retry-due",  # type: ignore[arg-type]
            next_check=RETRY,
        ),
    )

    result = CompositeDistributionPhaseExecutor._cache_result(view)

    assert isinstance(result, RunSwitchModelDownloadPendingResult)
    assert [item.code for item in result.blockers] == ["access_denied"]
    assert result.next_attempt_at == RETRY
    assert result.reason is None
