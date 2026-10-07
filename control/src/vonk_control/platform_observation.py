"""Read actual process provenance; publication availability owns no runtime fact."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from pydantic import Field, TypeAdapter
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from cluster_profiles.runtime_identity import (
    RuntimeBuildIdentity,
    packaged_runtime_identity,
)

from .models import ControlProcessHeartbeat
from .strict_json import StrictModel

Source = Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class ApiRuntimeObservation(StrictModel):
    source_sha: Source | None
    control_contract_sha256: Digest | None


class WorkerRuntimeObservation(StrictModel):
    process_instance_id: Digest
    source_sha: Source | None
    worker_contract_sha256: Digest | None
    loop_sequence: int = Field(ge=1)
    completed_at: datetime


class PlatformObservation(StrictModel):
    observed_at: datetime
    api: ApiRuntimeObservation
    workers: list[WorkerRuntimeObservation] | None
    worker_issue: (
        Literal["worker-observation-unavailable", "worker-provenance-unavailable"]
        | None
    )


@dataclass(frozen=True)
class CapturedPlatformObservation:
    identity: RuntimeBuildIdentity
    observation: PlatformObservation


class PlatformObserver:
    def __init__(
        self, sessions: sessionmaker[Session], *, clock: Callable[[], datetime]
    ):
        self._sessions = sessions
        self._clock = clock

    def read(self) -> PlatformObservation:
        return self.capture().observation

    def capture(self) -> CapturedPlatformObservation:
        now = self._clock().astimezone(UTC)
        identity = packaged_runtime_identity()
        with self._sessions() as session:
            rows = list(
                session.scalars(
                    select(ControlProcessHeartbeat).where(
                        ControlProcessHeartbeat.process_kind == "worker",
                        ControlProcessHeartbeat.loop_sequence >= 1,
                        ControlProcessHeartbeat.completed_at
                        >= now - timedelta(seconds=30),
                        ControlProcessHeartbeat.completed_at <= now,
                    )
                )
            )
        workers = [
            WorkerRuntimeObservation(
                process_instance_id=row.process_instance_id,
                source_sha=_readable_source(row.source_sha),
                worker_contract_sha256=_readable_digest(row.worker_contract_sha256),
                loop_sequence=row.loop_sequence,
                completed_at=(
                    row.completed_at.replace(tzinfo=UTC)
                    if row.completed_at.tzinfo is None
                    else row.completed_at.astimezone(UTC)
                ),
            )
            for row in rows
            if row.completed_at is not None
        ]
        observation = PlatformObservation(
            observed_at=now,
            api=ApiRuntimeObservation(
                source_sha=identity.source_sha,
                control_contract_sha256=identity.control_contract_sha256,
            ),
            workers=workers or None,
            worker_issue=(
                "worker-observation-unavailable"
                if not workers
                else "worker-provenance-unavailable"
                if any(
                    w.source_sha is None or w.worker_contract_sha256 is None
                    for w in workers
                )
                else None
            ),
        )
        return CapturedPlatformObservation(identity, observation)


def api_only_observation() -> PlatformObservation:
    return api_only_capture().observation


def api_only_capture() -> CapturedPlatformObservation:
    identity = packaged_runtime_identity()
    observation = PlatformObservation(
        observed_at=datetime.now(UTC),
        api=ApiRuntimeObservation(
            source_sha=identity.source_sha,
            control_contract_sha256=identity.control_contract_sha256,
        ),
        workers=None,
        worker_issue="worker-observation-unavailable",
    )
    return CapturedPlatformObservation(identity, observation)


def _readable_source(value: str | None) -> str | None:
    try:
        return TypeAdapter(Source | None).validate_python(value, strict=True)
    except (TypeError, ValueError):
        return None


def _readable_digest(value: str | None) -> str | None:
    try:
        return TypeAdapter(Digest | None).validate_python(value, strict=True)
    except (TypeError, ValueError):
        return None
