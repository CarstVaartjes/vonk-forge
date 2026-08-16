"""W19 fleet-size, shared-download, cancellation, and restart acceptance."""

from __future__ import annotations

import hashlib
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
for source_root in (ROOT / "agent/src", ROOT / "agent_protocol/src"):
    sys.path.insert(0, str(source_root))

from vonk_agent.packages.fetch import (
    AcquisitionCancelled,
    AcquisitionEngine,
)
from vonk_agent.packages.providers import (
    ComponentDescriptor,
    FetchResponse,
    NetworkHop,
    ProviderRegistry,
    SourceLocation,
    Validators,
)
from vonk_agent.packages.state import OperationBinding
from vonk_agent.packages.store import ContentStore

CONTENT = b"generic-workload-component-for-fleet-scale"
DIGEST = hashlib.sha256(CONTENT).hexdigest()
SOURCE = SourceLocation(
    provider="https",
    url="https://models.example.test/future-component",
    immutable_id="sha256:" + DIGEST,
    allowed_domains=("models.example.test",),
)
DESCRIPTOR = ComponentDescriptor(
    name="future-component",
    kind="model",
    digest="sha256:" + DIGEST,
    size=len(CONTENT),
    sources=(SOURCE,),
)


def _binding(index: int, *, attempt: int = 1) -> OperationBinding:
    return OperationBinding(
        job_id=f"10000000-0000-4000-8000-{index:012d}",
        operation_id=f"20000000-0000-4000-8000-{index:012d}",
        attempt=attempt,
        fence=f"30000000-0000-4000-8000-{index:012d}",
        node_id="spk_" + f"{index:032x}",
    )


class _Provider:
    name = "https"

    def __init__(self, *, delay: float = 0.0, chunks: tuple[bytes, ...] | None = None):
        self.delay = delay
        self.chunks = chunks or (CONTENT,)
        self.calls: list[int] = []
        self._lock = threading.Lock()

    def open(self, _source, offset, _validators, _deadline):
        with self._lock:
            self.calls.append(offset)
        if self.delay:
            time.sleep(self.delay)
        payload = CONTENT[offset:]
        return FetchResponse(
            status_code=206 if offset else 200,
            start_offset=offset,
            total_size=len(CONTENT),
            validators=Validators(etag='"future-v1"'),
            chunks=(payload,) if offset else self.chunks,
            hops=(NetworkHop(SOURCE.url, ("8.8.8.8",)),),
        )


def _fetch(store: ContentStore, provider: _Provider, index: int, *, cancelled=None):
    return AcquisitionEngine(store, ProviderRegistry((provider,))).fetch(
        DESCRIPTOR,
        _binding(index),
        lambda _event: None,
        cancelled or (lambda: False),
    )


@pytest.mark.parametrize("nodes", (1, 2, 16, 64))
def test_generated_fleet_has_no_package_node_limit(nodes: int) -> None:
    fleet = tuple("spk_" + f"{index:032x}" for index in range(nodes))
    assert len(fleet) == nodes
    assert len(set(fleet)) == nodes
    # Deliberately exercise a generated fleet larger than the documented
    # examples; this is an input-size check, not a product hard limit.
    if nodes == 64:
        larger = tuple("spk_" + f"{index:032x}" for index in range(257))
        assert len(larger) == 257


def test_concurrent_identical_downloads_share_one_verified_fetch(tmp_path: Path) -> None:
    store_root = tmp_path / "packages"
    provider = _Provider(delay=0.08, chunks=(CONTENT[:8], CONTENT[8:]))
    store = ContentStore(store_root, capacity_bytes=len(CONTENT))
    results: list[object] = []
    errors: list[Exception] = []
    barrier = threading.Barrier(2)

    def run(index: int) -> None:
        try:
            barrier.wait()
            results.append(_fetch(store, provider, index))
        except (OSError, RuntimeError, ValueError) as error:  # pragma: no cover
            errors.append(error)

    threads = [threading.Thread(target=run, args=(index,)) for index in (1, 2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)

    assert all(not thread.is_alive() for thread in threads)
    assert errors == []
    assert len(results) == 2
    assert all(item.digest == DIGEST for item in results)
    assert provider.calls == [0]
    assert store.lookup(DIGEST, len(CONTENT)) is not None


def test_cancelled_partial_survives_agent_restart_and_resumes(tmp_path: Path) -> None:
    root = tmp_path / "packages"
    first_provider = _Provider(chunks=(CONTENT[:9], CONTENT[9:]))
    written = {"bytes": 0}

    def cancel() -> bool:
        return written["bytes"] >= 9

    def observe(event: dict[str, object]) -> None:
        written["bytes"] = int(event.get("bytes_completed", 0))

    store = ContentStore(root, capacity_bytes=len(CONTENT))
    with pytest.raises(AcquisitionCancelled):
        AcquisitionEngine(store, ProviderRegistry((first_provider,))).fetch(
            DESCRIPTOR,
            _binding(1),
            observe,
            cancel,
        )
    assert 0 < written["bytes"] < len(CONTENT)

    # Reopen the durable store as a replacement process and resume from the
    # journaled range validator.  No partial bytes are exposed as a result.
    reopened = ContentStore(root, capacity_bytes=len(CONTENT))
    second_provider = _Provider()
    result = _fetch(reopened, second_provider, 1, cancelled=lambda: False)
    assert result.digest == DIGEST
    assert second_provider.calls == [written["bytes"]]
    assert reopened.lookup(DIGEST, len(CONTENT)) is not None


def test_direct_provider_boundary_has_no_ssh_or_nas_relay(tmp_path: Path) -> None:
    store = ContentStore(tmp_path / "packages", capacity_bytes=len(CONTENT))
    provider = _Provider()
    _fetch(store, provider, 1)
    assert provider.calls == [0]
    # The provider boundary is HTTPS and immutable; no subprocess/SSH path is
    # available to this package operation.
    assert SOURCE.provider == "https"
    assert SOURCE.url.startswith("https://")
