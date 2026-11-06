"""An upload waiting on the reference flock must not stall the event loop."""

from __future__ import annotations

import asyncio
import hashlib
import threading
from collections.abc import AsyncIterator
from pathlib import Path

from vonk_control.artifact_blob_store import ArtifactBlobStore, StoredArtifactBlob

TICKS = 20
HANG_GUARD_SECONDS = 60


def test_upload_waiting_on_reference_lock_leaves_the_event_loop_running(
    tmp_path: Path,
) -> None:
    store = ArtifactBlobStore(tmp_path / "blobs", max_stored_bytes=1024**2)
    content = b"job output bytes"
    digest = hashlib.sha256(content).hexdigest()
    held = threading.Event()
    release = threading.Event()

    def hold_exclusive() -> None:
        with store.reference_reconciliation():
            held.set()
            release.wait(HANG_GUARD_SECONDS)

    async def chunks() -> AsyncIterator[bytes]:
        yield content

    async def upload() -> StoredArtifactBlob:
        async with store.reference_attachment_async():
            return await store.put_stream(
                digest, chunks(), expected_bytes=len(content), maximum_bytes=1024
            )

    async def scenario() -> tuple[int, StoredArtifactBlob]:
        holder = threading.Thread(target=hold_exclusive)
        holder.start()
        assert held.wait(HANG_GUARD_SECONDS)
        uploading = asyncio.create_task(upload())
        # Stand-in for any other request on the same loop: it keeps being
        # served while the upload is parked on the flock.
        ticks = 0
        for _ in range(TICKS):
            await asyncio.sleep(0.01)
            ticks += 1
        assert not uploading.done()  # still waiting for the flock
        release.set()
        stored = await uploading
        holder.join(HANG_GUARD_SECONDS)
        return ticks, stored

    ticks, stored = asyncio.run(scenario())

    assert ticks == TICKS
    assert store.resolve(stored.storage_key, digest, len(content)) == stored.path
    assert stored.path.read_bytes() == content
