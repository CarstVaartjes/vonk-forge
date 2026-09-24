"""Synchronous streaming facade over cancellable HTTPS I/O.

One deadline covers connection, request delivery, headers, and every body read.
The caller still owns response size, media, and canonical contract validation.
"""

from __future__ import annotations

import asyncio
import io
import socket
import threading
import urllib.error
import urllib.request
from collections.abc import AsyncIterator, Coroutine
from concurrent.futures import Future
from email.message import Message
from typing import IO, Any, Self, cast

import httpx

_DNS_LOCK = threading.Lock()
_DNS_PENDING: dict[tuple[object, ...], Future[Any]] = {}


def _resolve_address(arguments: tuple[Any, ...]) -> Future[Any]:
    """Share only in-flight system lookups; completed answers are not cached."""

    with _DNS_LOCK:
        if existing := _DNS_PENDING.get(arguments):
            return existing
        result: Future[Any] = Future()
        _DNS_PENDING[arguments] = result

    def resolve() -> None:
        try:
            result.set_result(socket.getaddrinfo(*arguments))
        except Exception as error:  # noqa: BLE001 - propagate worker failures to the waiting request
            result.set_exception(error)
        finally:
            with _DNS_LOCK:
                _DNS_PENDING.pop(arguments)

    try:
        threading.Thread(target=resolve, name="vonk-dns", daemon=True).start()
    except (RuntimeError, OSError) as error:
        with _DNS_LOCK:
            _DNS_PENDING.pop(arguments)
        result.set_exception(error)
    return result


class _HTTPSLoop(asyncio.SelectorEventLoop):
    async def getaddrinfo(self, host, port, *, family=0, type=0, proto=0, flags=0):
        # The system resolver cannot be interrupted. Its worker may finish
        # later, but only returns addresses: it never opens a connection or
        # receives credentials. Cancelling the task discards that late answer.
        # Unlike asyncio's default executor, this worker does not hold process
        # shutdown open while an expired request waits for system DNS.
        result = _resolve_address((host, port, family, type, proto, flags))
        return await asyncio.shield(asyncio.wrap_future(result))


class HTTPSResponse(io.BufferedIOBase):
    def __init__(self, request: urllib.request.Request, timeout: float) -> None:
        self._runner = asyncio.Runner(loop_factory=_HTTPSLoop)
        self._deadline = self._runner.get_loop().time() + timeout
        self._client: httpx.AsyncClient | None = None
        self._response: httpx.Response | None = None
        self._body: AsyncIterator[bytes] | None = None
        self._pending = b""
        self._ended = False
        self._closed = False
        try:
            self._response = self._run(self._open(request, timeout))
        except BaseException:
            self.close()
            raise
        assert self._response is not None
        self.status = self._response.status_code
        self.headers = Message()
        for name, value in self._response.headers.multi_items():
            self.headers[name] = value

    def _run[T](self, work: Coroutine[Any, Any, T]) -> T:
        async def bounded() -> T:
            async with asyncio.timeout_at(self._deadline):
                return await work

        try:
            return self._runner.run(bounded())
        except (httpx.HTTPError, OSError) as error:
            # Keep the typed cause for safe classification, never its raw text.
            raise urllib.error.URLError(error) from None

    async def _open(
        self, request: urllib.request.Request, timeout: float
    ) -> httpx.Response:
        self._client = httpx.AsyncClient(
            timeout=timeout, follow_redirects=False, verify=True
        )
        data = request.data
        content: bytes | AsyncIterator[bytes] | None
        if data is None or isinstance(data, bytes):
            content = data
        elif isinstance(data, io.BufferedIOBase):
            # Artifact inputs are already opened and verified by their owner.
            # Read bounded chunks; never materialize the entire file in memory.
            async def chunks() -> AsyncIterator[bytes]:
                while chunk := data.read(64 * 1024):
                    yield chunk
                    await asyncio.sleep(0)

            content = chunks()
        else:
            raise TypeError("HTTPS request body must be bytes or an open binary file")
        outgoing = self._client.build_request(
            request.get_method(),
            request.full_url,
            headers={"Accept-Encoding": "identity", **dict(request.header_items())},
            content=content,
        )
        self._response = await self._client.send(outgoing, stream=True)
        self._body = self._response.aiter_raw()
        return self._response

    async def _read(self, amount: int) -> bytes:
        assert self._body is not None
        result = bytearray()
        while len(result) < amount:
            if self._pending:
                length = min(amount - len(result), len(self._pending))
                result.extend(self._pending[:length])
                self._pending = self._pending[length:]
            elif self._ended:
                break
            else:
                try:
                    self._pending = await anext(self._body)
                except StopAsyncIteration:
                    self._ended = True
        return bytes(result)

    def read(self, amount: int = -1, /) -> bytes:
        if amount < 0:
            raise ValueError("HTTPS reads must have a byte bound")
        if self._closed:
            raise ValueError("HTTPS response is closed")
        return self._run(self._read(amount))

    @property
    def closed(self) -> bool:
        return self._closed

    async def _close(self) -> None:
        if self._response is not None:
            await self._response.aclose()
        if self._client is not None:
            await self._client.aclose()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._runner.run(self._close())
        finally:
            self._runner.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def open_https(request: urllib.request.Request, *, timeout: float) -> HTTPSResponse:
    response = HTTPSResponse(request, timeout)
    if response.status >= 300:
        # Preserve the existing injected opener boundary for all consumers,
        # including streaming artifact transfers, without following redirects.
        raise urllib.error.HTTPError(
            request.full_url,
            response.status,
            "control HTTP response",
            response.headers,
            # urllib consumes only read/close/closed, provided by this stream.
            cast(IO[bytes], response),
        )
    return response
