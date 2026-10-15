import time

import httpx2
import vonk_control.model_cache.download as download_module

from .test_model_cache import (  # noqa: F401
    _download,
    _http_artifact,
    _http_cache_service,
    cache,
)


def test_slow_finalization_after_the_last_byte_completes_without_retry(
    cache,  # noqa: F811
    tmp_path,
    monkeypatch,
) -> None:
    """Local work after every byte arrived is not a stalled source.

    A huge object's range assembly can outlast the source-idle deadline; the
    attempt still completes in one pass and never refetches its bytes.
    """
    _existing, sessions = cache
    payload = bytes(range(256)) * 256
    monkeypatch.setattr(
        "vonk_control.model_cache.constants._PARALLEL_RANGE_MIN_BYTES", 1
    )
    monkeypatch.setattr(
        "vonk_control.model_cache.split_transfer._TRANSFER_CLAIM_SECONDS", 1
    )
    real_download_ranges = download_module.download_ranges

    def slow_finalization(*args, **kwargs):
        completed = real_download_ranges(*args, **kwargs)
        time.sleep(2.5)  # longer than the deadline, after the last byte
        return completed

    monkeypatch.setattr(download_module, "download_ranges", slow_finalization)
    requests = []

    def handler(request):
        requests.append(request.headers["range"])
        start, end = map(
            int, request.headers["range"].removeprefix("bytes=").split("-")
        )
        return httpx2.Response(
            206,
            request=request,
            content=payload[start : end + 1],
            headers={"content-range": f"bytes {start}-{end}/{len(payload)}"},
        )

    service, client = _http_cache_service(tmp_path, sessions, handler)
    artifact = _http_artifact(payload)
    try:
        operation = _download(
            service,
            [artifact],
            model_content_sha256="b" * 64,
            request_key="00000000-0000-4000-8000-000000002001",
        )
        assert operation.state == "succeeded", operation.last_error
        assert service._object_path(str(artifact["sha256"])).read_bytes() == payload
        assert len(requests) == len(set(requests)) == 4  # every byte fetched once
    finally:
        service.close()
        client.close()
