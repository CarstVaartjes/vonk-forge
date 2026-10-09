"""Bounded qualification observation and durable UUID request identities."""

from __future__ import annotations

import copy
import fcntl
import hashlib
import math
import os
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .control_client import ControlClientError, ControlForbidden, ControlUnauthorized
from .qualification_fixtures import FixtureError


def _request_root(directory: Path | None) -> Path:
    return (
        directory
        if directory is not None
        else Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache")))
        / "vonkctl/qualification-requests"
    )


def _durable_key(directory: Path | None, scope: str) -> str:
    """A UUID filename retains identity even when checkpoint content is torn."""
    root = _request_root(directory) / hashlib.sha256(scope.encode()).hexdigest()
    root.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(root / ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise QualificationObservationUnknown(
                "qualification request owner is publishing identity"
            ) from error
        for path in root.iterdir():
            try:
                key = str(uuid.UUID(path.name))
                directory_descriptor = os.open(root, os.O_RDONLY)
                try:
                    os.fsync(directory_descriptor)
                finally:
                    os.close(directory_descriptor)
                return key
            except ValueError:
                continue
        key = str(uuid.uuid4())
        key_descriptor = os.open(
            root / key, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
        )
        try:
            os.fsync(key_descriptor)
        finally:
            os.close(key_descriptor)
        directory_descriptor = os.open(root, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
        return key
    finally:
        os.close(descriptor)


def _forget_key(directory: Path | None, scope: str, key: str) -> None:
    root = _request_root(directory) / hashlib.sha256(scope.encode()).hexdigest()
    try:
        (root / key).unlink(missing_ok=True)
    except OSError:
        pass  # terminal identity remains observable; never holds remote resources


def _durable_deadline(
    directory: Path | None,
    scope: str,
    key: str,
    budget: float,
    clock: Callable[[], float],
) -> float:
    """A wall-clock expiry is fixed before effects and survives process restart."""
    path = (
        _request_root(directory)
        / hashlib.sha256(scope.encode()).hexdigest()
        / f"{key}.deadline"
    )
    try:
        expiry = float(path.read_text())
        if not math.isfinite(expiry):
            return clock()  # damaged bookkeeping permits observation only
    except FileNotFoundError:
        expiry = time.time() + budget
        try:
            with path.open("x") as output:
                output.write(str(expiry))
                output.flush()
                os.fsync(output.fileno())
            descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except FileExistsError:
            try:
                expiry = float(path.read_text())
                if not math.isfinite(expiry):
                    return clock()
            except (OSError, ValueError):
                return clock()
    except (OSError, ValueError):
        return clock()
    return clock() + max(0, expiry - time.time())


class QualificationError(RuntimeError):
    """A bounded qualification observation ended without success."""


class QualificationObservationUnknown(ValueError):
    """Peer or checkpoint data has not yet yielded a confirmed observation."""


class QualificationQualityFailure(QualificationError):
    """A measured assertion failure, scoped to a single case."""


class _BudgetClient:
    """Every request/transfer shares the caller's remaining total budget."""

    def __init__(self, client: Any, deadline: float, clock: Callable[[], float]):
        self.client, self.deadline, self.clock = client, deadline, clock

    @property
    def request_timeout_seconds(self) -> float:
        remaining = self.deadline - self.clock()
        if remaining <= 0:
            raise QualificationError("qualification observation deadline elapsed")
        return min(getattr(self.client, "request_timeout_seconds", 30), remaining)

    def request(self, method, path, payload=None, **kwargs):
        kwargs["timeout_seconds"] = min(
            kwargs.get("timeout_seconds") or self.request_timeout_seconds,
            self.request_timeout_seconds,
        )
        return self.client.request(method, path, payload, **kwargs)

    def profile_endpoints(self, number, alias=None):
        return self.client.profile_endpoints(number, alias)

    def upload_file(self, *args, **kwargs):
        # Transfers use a new client with the remaining deadline, never the
        # original normal timeout. Injected test peers implement this boundary.
        from .control_client import ControlClient

        client = self.client
        if isinstance(client, ControlClient):
            client = copy.copy(client)
            client._artifact_transfer_timeout = self.request_timeout_seconds
        return client.upload_file(*args, **kwargs)

    def download_file(self, *args, **kwargs):
        from .control_client import ControlClient

        client = self.client
        if isinstance(client, ControlClient):
            client = copy.copy(client)
            client._artifact_transfer_timeout = self.request_timeout_seconds
        return client.download_file(*args, **kwargs)


def _observe(work, deadline, clock, sleeper, interval):
    """Unknown peers re-observe in one budget; auth refusals never retry."""
    while clock() < deadline:
        try:
            return work()
        except (ControlUnauthorized, ControlForbidden):
            raise
        except FixtureError:
            raise
        except (ControlClientError, OSError, ValueError, KeyError, TypeError):
            remaining = deadline - clock()
            if remaining > 0:
                sleeper(min(interval, remaining))
    raise QualificationError(
        "qualification observation remained unknown at its deadline"
    )
