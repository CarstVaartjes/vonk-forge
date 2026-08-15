from __future__ import annotations

import pytest
from vonk_control.harness_conformance import (
    HarnessConformanceError,
    SyntheticLifecycleDriver,
    run_synthetic_conformance,
)
from vonk_control.harnesses import BUILTIN_HARNESS_SLUGS


@pytest.mark.parametrize("slug", BUILTIN_HARNESS_SLUGS)
def test_harness_completes_observed_synthetic_lifecycle(slug: str) -> None:
    driver: SyntheticLifecycleDriver | None = None

    def factory(request, clock):
        nonlocal driver
        driver = SyntheticLifecycleDriver(request, clock=clock)
        return driver

    evidence = run_synthetic_conformance(slug, driver_factory=factory)

    assert driver is not None
    assert tuple(driver.calls) == evidence.phases
    assert evidence.phases == (
        "inspect",
        "prepare",
        "verify",
        "start",
        "inspect",
        "inspect",
        "start",
        "ready",
        "invoke",
        "inspect",
        "stop",
        "inspect",
        "inspect",
        "stop",
        "verify-stopped",
    )
    assert evidence.offline_runtime is True
    assert evidence.security["docker_socket"] is False
    assert evidence.interrupted_start_recovered is True
    assert evidence.interrupted_stop_recovered is True
    assert evidence.stop_bounded is True
    assert evidence.document["schema_version"] == 1


class NonIdempotentInspectDriver(SyntheticLifecycleDriver):
    def inspect(self):
        observed = dict(super().inspect())
        observed["nonce"] = len(self.calls)
        return observed


class OnlineInvocationDriver(SyntheticLifecycleDriver):
    def invoke(self):
        observed = dict(super().invoke())
        observed["offline"] = False
        return observed


class SlowStopDriver(SyntheticLifecycleDriver):
    def stop(self, deadline: float):
        observed = super().stop(deadline)
        self.clock.advance(31)
        return observed


class InvalidEvidenceDriver(SyntheticLifecycleDriver):
    def verify_stopped(self):
        observed = dict(super().verify_stopped())
        document = dict(observed["evidence"])
        document["schema_version"] = 2
        observed["evidence"] = document
        return observed


class SocketExposureDriver(SyntheticLifecycleDriver):
    def prepare(self):
        observed = dict(super().prepare())
        security = dict(observed["security"])
        security["docker_socket"] = True
        observed["security"] = security
        return observed


class UnverifiedDriver(SyntheticLifecycleDriver):
    def verify(self):
        observed = dict(super().verify())
        observed["verified"] = False
        return observed


class UnreadyDriver(SyntheticLifecycleDriver):
    def ready(self):
        observed = dict(super().ready())
        observed["ready"] = False
        return observed


class WrongStartResultDriver(SyntheticLifecycleDriver):
    def start(self):
        observed = dict(super().start())
        observed["state"] = "prepared"
        return observed


class WrongStopResultDriver(SyntheticLifecycleDriver):
    def stop(self, deadline: float):
        observed = dict(super().stop(deadline))
        observed["state"] = "running"
        return observed


@pytest.mark.parametrize(
    ("factory", "message"),
    [
        (
            lambda request, clock: NonIdempotentInspectDriver(request, clock=clock),
            "idempotent",
        ),
        (
            lambda request, clock: OnlineInvocationDriver(request, clock=clock),
            "offline",
        ),
        (lambda request, clock: SlowStopDriver(request, clock=clock), "deadline"),
        (
            lambda request, clock: InvalidEvidenceDriver(request, clock=clock),
            "evidence",
        ),
        (
            lambda request, clock: SocketExposureDriver(request, clock=clock),
            "security",
        ),
        (lambda request, clock: UnverifiedDriver(request, clock=clock), "verify"),
        (lambda request, clock: UnreadyDriver(request, clock=clock), "ready"),
        (lambda request, clock: WrongStartResultDriver(request, clock=clock), "start"),
        (lambda request, clock: WrongStopResultDriver(request, clock=clock), "stop"),
    ],
)
def test_conformance_rejects_broken_lifecycle_drivers(factory, message: str) -> None:
    with pytest.raises(HarnessConformanceError, match=message):
        run_synthetic_conformance("vllm", driver_factory=factory)


def test_conformance_fails_closed_for_unknown_harness() -> None:
    with pytest.raises(HarnessConformanceError, match="unknown execution harness"):
        run_synthetic_conformance("legacy-harness")
