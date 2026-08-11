from __future__ import annotations

import hashlib
from dataclasses import replace

import pytest
from vonk_agent.packages.fetch import (
    AcquisitionCancelled,
    AcquisitionEngine,
    AcquisitionError,
    DownloadRecord,
    Reservation,
    StoreObject,
)
from vonk_agent.packages.providers import (
    ComponentDescriptor,
    FetchResponse,
    NetworkHop,
    ProviderRegistry,
    SourceLocation,
    Validators,
)

DATA = b"0123456789abcdef"
DIGEST = hashlib.sha256(DATA).hexdigest()


class Store:
    def __init__(self) -> None:
        self.objects: dict[str, StoreObject] = {}
        self.partial = bytearray()
        self.validators = Validators()
        self.reservations = 0
        self.checkpoints: list[int] = []
        self.quarantines: list[str] = []
        self.promotions = 0

    def lookup(self, digest: str, size: int):
        found = self.objects.get(digest)
        return found if found is not None and found.size == size else None

    def reserve(self, _binding, bytes_required: int) -> Reservation:
        self.reservations += 1
        return Reservation("reservation-1", bytes_required)

    def release(self, _reservation: Reservation) -> None:
        self.reservations -= 1

    def begin_component(self, _reservation, descriptor) -> DownloadRecord:
        return DownloadRecord(
            component=descriptor.name,
            digest=descriptor.digest.removeprefix("sha256:"),
            expected_size=descriptor.size,
            bytes_completed=len(self.partial),
            validators=self.validators,
        )

    def iter_partial(self, _record: DownloadRecord):
        for offset in range(0, len(self.partial), 3):
            yield bytes(self.partial[offset : offset + 3])

    def append_partial(self, _record: DownloadRecord, chunk: bytes) -> None:
        self.partial.extend(chunk)

    def checkpoint(self, _record, bytes_completed: int, validators: Validators) -> None:
        assert bytes_completed == len(self.partial)
        self.validators = validators
        self.checkpoints.append(bytes_completed)

    def reset_partial(self, _record, reason: str) -> DownloadRecord:
        self.partial.clear()
        self.validators = Validators()
        self.quarantines.append(reason)
        return replace(_record, bytes_completed=0, validators=Validators())

    def quarantine_partial(self, record, reason: str) -> DownloadRecord:
        return self.reset_partial(record, reason)

    def pause(self, _record) -> None:
        pass

    def promote_component(self, record, verified_digest: str) -> StoreObject:
        assert bytes(self.partial) == DATA
        assert verified_digest == DIGEST
        result = StoreObject(DIGEST, len(DATA), "blob", f"objects/sha256/{DIGEST}")
        self.objects[DIGEST] = result
        self.partial.clear()
        self.promotions += 1
        return result


class Provider:
    name = "https"

    def __init__(self, responses) -> None:
        self.responses = list(responses)
        self.calls = []

    def open(self, source, offset, validators, _deadline):
        self.calls.append((source, offset, validators))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _source(host: str = "models.example.test") -> SourceLocation:
    return SourceLocation(
        provider="https",
        url=f"https://{host}/model.bin",
        immutable_id=f"sha256:{DIGEST}",
        allowed_domains=(host,),
    )


def _descriptor(*sources: SourceLocation) -> ComponentDescriptor:
    return ComponentDescriptor(
        name="model-weights",
        kind="model",
        digest=f"sha256:{DIGEST}",
        size=len(DATA),
        sources=sources or (_source(),),
        unpacked_size=len(DATA),
    )


def _response(
    content: bytes,
    *,
    start: int = 0,
    total: int = len(DATA),
    etag: str = '"version-1"',
) -> FetchResponse:
    return FetchResponse(
        status_code=206 if start else 200,
        start_offset=start,
        total_size=total,
        validators=Validators(etag=etag),
        chunks=tuple(content[index : index + 3] for index in range(0, len(content), 3)),
        hops=(NetworkHop("https://models.example.test/model.bin", ("8.8.8.8",)),),
    )


def _engine(store: Store, provider: Provider) -> AcquisitionEngine:
    return AcquisitionEngine(store, ProviderRegistry((provider,)))


def test_fetch_streams_progress_and_promotes_only_after_exact_verification() -> None:
    store = Store()
    provider = Provider((_response(DATA),))
    reports = []

    result = _engine(store, provider).fetch(
        _descriptor(), "binding", reports.append, lambda: False
    )

    assert result.digest == DIGEST
    assert store.promotions == 1
    assert store.reservations == 0
    assert reports[-1] == {
        "phase": "fetch",
        "component": "model-weights",
        "bytes_completed": len(DATA),
        "bytes_total": len(DATA),
        "objects_completed": 1,
        "objects_total": 1,
        "cache_hits": 0,
        "reserved_bytes": len(DATA),
    }


def test_restart_hashes_existing_prefix_and_resumes_with_if_range_validator() -> None:
    store = Store()
    store.partial.extend(DATA[:7])
    store.validators = Validators(etag='"version-1"')
    provider = Provider((_response(DATA[7:], start=7),))

    result = _engine(store, provider).fetch(
        _descriptor(), "binding", lambda _event: None, lambda: False
    )

    assert result.digest == DIGEST
    assert provider.calls == [(_source(), 7, Validators(etag='"version-1"'))]


@pytest.mark.parametrize("changed_etag", (False, True))
def test_ignored_range_or_changed_validator_restarts_from_zero(
    changed_etag: bool,
) -> None:
    store = Store()
    store.partial.extend(DATA[:5])
    store.validators = Validators(etag='"old"')
    first = _response(
        DATA,
        start=0 if not changed_etag else 5,
        etag='"new"' if changed_etag else '"old"',
    )
    provider = Provider((first, _response(DATA, etag='"new"')))

    _engine(store, provider).fetch(
        _descriptor(), "binding", lambda _event: None, lambda: False
    )

    assert [call[1] for call in provider.calls] == [5, 0]
    assert store.quarantines
    assert store.promotions == 1


def test_cancellation_keeps_durable_partial_and_restart_resumes() -> None:
    store = Store()
    provider = Provider((_response(DATA),))
    cancelled = False

    def cancellation() -> bool:
        nonlocal cancelled
        if len(store.partial) >= 6:
            cancelled = True
        return cancelled

    with pytest.raises(AcquisitionCancelled, match="cancelled"):
        _engine(store, provider).fetch(
            _descriptor(), "binding", lambda _event: None, cancellation
        )

    completed = len(store.partial)
    assert 0 < completed < len(DATA)
    assert store.checkpoints[-1] == completed
    assert store.reservations == 0

    resumed = Provider((_response(DATA[completed:], start=completed),))
    assert (
        _engine(store, resumed)
        .fetch(_descriptor(), "binding", lambda _event: None, lambda: False)
        .digest
        == DIGEST
    )


def test_mirror_failover_never_changes_expected_digest() -> None:
    first = _source("mirror-one.example.test")
    second = _source("mirror-two.example.test")
    provider = Provider((AcquisitionError("source unavailable"), _response(DATA)))
    store = Store()

    result = _engine(store, provider).fetch(
        _descriptor(first, second), "binding", lambda _event: None, lambda: False
    )

    assert result.digest == DIGEST
    assert [call[0] for call in provider.calls] == [first, second]


def test_mirror_failover_reloads_the_durable_checkpoint() -> None:
    first = _source("mirror-one.example.test")
    second = _source("mirror-two.example.test")

    def interrupted_stream():
        yield DATA[:6]
        raise OSError("connection dropped")

    interrupted = replace(_response(b""), chunks=interrupted_stream())
    provider = Provider((interrupted, _response(DATA[6:], start=6)))
    store = Store()

    result = _engine(store, provider).fetch(
        _descriptor(first, second), "binding", lambda _event: None, lambda: False
    )

    assert result.digest == DIGEST
    assert [call[1] for call in provider.calls] == [0, 6]


def test_checksum_mismatch_is_quarantined_and_never_promoted() -> None:
    store = Store()
    provider = Provider((_response(b"x" * len(DATA)),))

    with pytest.raises(AcquisitionError, match="model-weights") as caught:
        _engine(store, provider).fetch(
            _descriptor(), "binding", lambda _event: None, lambda: False
        )

    assert "models.example.test" not in str(caught.value)
    assert store.quarantines == ["digest-mismatch"]
    assert store.promotions == 0
    assert store.objects == {}


@pytest.mark.parametrize(
    "response",
    (
        _response(DATA, total=len(DATA) + 1),
        _response(DATA + b"overflow"),
        _response(DATA[:-1]),
    ),
)
def test_declared_and_observed_size_are_exactly_bounded(
    response: FetchResponse,
) -> None:
    store = Store()
    with pytest.raises(AcquisitionError, match="model-weights"):
        _engine(store, Provider((response,))).fetch(
            _descriptor(), "binding", lambda _event: None, lambda: False
        )
    assert store.promotions == 0


def test_cache_hit_does_not_reserve_or_open_provider() -> None:
    store = Store()
    cached = StoreObject(DIGEST, len(DATA), "blob", f"objects/sha256/{DIGEST}")
    store.objects[DIGEST] = cached
    provider = Provider(())
    reports = []

    assert (
        _engine(store, provider).fetch(
            _descriptor(), "binding", reports.append, lambda: False
        )
        == cached
    )
    assert provider.calls == []
    assert store.reservations == 0
    assert reports[-1]["cache_hits"] == 1


def test_component_descriptor_bounds_archive_expansion_and_source_count() -> None:
    with pytest.raises(ValueError, match="unpacked size"):
        replace(_descriptor(), unpacked_size=len(DATA) * 1000)
    with pytest.raises(ValueError, match="sources"):
        replace(_descriptor(), sources=tuple(_source() for _ in range(9)))
