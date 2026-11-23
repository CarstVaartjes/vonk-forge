"""Job uploads parked on the reference flock leave the event loop running."""

from __future__ import annotations

import asyncio
import hashlib
import threading
from collections.abc import AsyncIterator, Awaitable, Callable

import pytest
from vonk_control.artifact_jobs.contracts import ArtifactJobView

from .test_artifact_jobs import (
    artifact_create_request,
    create_artifact_job,
    running_artifact_service,
    submitted_artifact_job,
)

TICKS = 20
HANG_GUARD_SECONDS = 60


def _scenario(
    store_lock: Callable[[], object],
    start_upload: Callable[[], object],
) -> int:
    """Park an upload on the exclusive reference lock and count served ticks."""
    held = threading.Event()
    release = threading.Event()

    def hold_exclusive() -> None:
        with store_lock():  # type: ignore[attr-defined]
            held.set()
            release.wait(HANG_GUARD_SECONDS)

    async def run() -> int:
        holder = threading.Thread(target=hold_exclusive)
        holder.start()
        assert held.wait(HANG_GUARD_SECONDS)
        uploading = asyncio.ensure_future(start_upload())  # type: ignore[arg-type]
        ticks = 0
        for _ in range(TICKS):
            await asyncio.sleep(0.01)
            ticks += 1
        assert not uploading.done()  # still parked on the flock
        release.set()
        await asyncio.wait_for(uploading, HANG_GUARD_SECONDS)
        holder.join(HANG_GUARD_SECONDS)
        return ticks

    return asyncio.run(run())


@pytest.mark.parametrize("direction", ["input", "output"])
def test_upload_parked_on_the_reference_lock_does_not_stall_the_loop(
    tmp_path, postgres_engine, direction
) -> None:
    _sessions, _operations, _queue, service, run_id, node_id = running_artifact_service(
        tmp_path, engine=postgres_engine
    )
    content = b"png"
    digest = hashlib.sha256(content).hexdigest()

    async def chunks() -> AsyncIterator[bytes]:
        yield content

    upload: Callable[[], Awaitable[object]]
    if direction == "input":
        job = create_artifact_job(
            service,
            **artifact_create_request(run_id, "00000000-0000-4000-8000-000000000201"),
        )

        async def upload_input() -> ArtifactJobView:
            return await service.put_input_stream(
                job.id,
                name="input.png",
                media_type="image/png",
                expected_sha256=digest,
                content_length=len(content),
                chunks=chunks(),
            )

        upload = upload_input

    else:
        job = submitted_artifact_job(service, run_id, request_suffix=210)
        output = b"{}"
        output_digest = hashlib.sha256(output).hexdigest()

        async def output_chunks() -> AsyncIterator[bytes]:
            yield output

        async def upload_output() -> None:
            await service.put_output_stream(
                job.id,
                node_id=node_id,
                name="output.png",
                media_type="image/png",
                expected_sha256=output_digest,
                content_length=len(output),
                chunks=output_chunks(),
            )

        upload = upload_output

    ticks = _scenario(service._blob_store.reference_reconciliation, upload)

    assert ticks == TICKS
