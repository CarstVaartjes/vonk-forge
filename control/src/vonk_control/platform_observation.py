"""Read actual process provenance; publication availability owns no runtime fact."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from pydantic import Field, TypeAdapter, ValidationError
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from cluster_profiles.runtime_identity import (
    RuntimeBuildIdentity,
    packaged_runtime_identity,
)

from .capabilities import CapabilityRegistry
from .capability_contract import CapabilityStatus
from .models import ControlProcessHeartbeat, FleetProfileApplication
from .observation_capture import begin_observation_capture
from .platform_observation_errors import ObservationCaptureUnavailable
from .progress_stall import WAITING_STATES, is_stalled, read_stall
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


class StalledApplication(StrictModel):
    """A profile load that keeps repeating the same wait with no progress."""

    application_id: str
    profile_id: str
    state: str
    detail: str
    repeats: int = Field(ge=1)
    first_seen_at: datetime


class PlatformObservation(StrictModel):
    capabilities: list[CapabilityStatus] = Field(default_factory=list)
    #: Informative only: nothing is cancelled, and a load leaves this list
    #: the moment it moves again.
    stalled_applications: list[StalledApplication] = Field(default_factory=list)
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
        self,
        sessions: sessionmaker[Session],
        *,
        clock: Callable[[], datetime],
        capabilities: CapabilityRegistry | None = None,
    ):
        self._capabilities = capabilities
        self._sessions = sessions
        self._clock = clock

    def read(self) -> PlatformObservation:
        return self.capture().observation

    def capture(self) -> CapturedPlatformObservation:
        try:
            return self._capture()
        except ValidationError as error:
            raise ObservationCaptureUnavailable(
                phase="stored-worker-validation"
            ) from error
        except SQLAlchemyError as error:
            raise ObservationCaptureUnavailable(phase="database-capture") from error

    def _capture(self) -> CapturedPlatformObservation:
        now = self._clock().astimezone(UTC)
        identity = packaged_runtime_identity()
        with self._sessions() as session:
            begin_observation_capture(session)
            rows = list(
                session.scalars(
                    select(ControlProcessHeartbeat)
                    .where(
                        ControlProcessHeartbeat.process_kind == "worker",
                        ControlProcessHeartbeat.loop_sequence >= 1,
                        ControlProcessHeartbeat.completed_at
                        >= now - timedelta(seconds=30),
                        ControlProcessHeartbeat.completed_at <= now,
                    )
                    .order_by(ControlProcessHeartbeat.process_instance_id)
                )
            )
            stalled = _stalled_applications(session, now)
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
            capabilities=self._capabilities.statuses() if self._capabilities else [],
            stalled_applications=stalled,
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


def _stalled_applications(session: Session, now: datetime) -> list[StalledApplication]:
    rows = session.execute(
        select(
            FleetProfileApplication.id,
            FleetProfileApplication.profile_id,
            FleetProfileApplication.state,
            FleetProfileApplication.progress,
        )
        .where(FleetProfileApplication.state.in_(sorted(WAITING_STATES)))
        .order_by(FleetProfileApplication.id)
    )
    stalled: list[StalledApplication] = []
    for application_id, profile_id, state, progress in rows:
        stall = read_stall(
            progress.get("stall") if isinstance(progress, Mapping) else None
        )
        if is_stalled(stall, now=now):
            stalled.append(
                StalledApplication(
                    application_id=application_id,
                    profile_id=profile_id,
                    state=state,
                    detail=stall.detail,
                    repeats=stall.repeats,
                    first_seen_at=stall.first_seen_at,
                )
            )
    return stalled


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
