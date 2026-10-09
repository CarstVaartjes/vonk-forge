"""Unknown filesystem effects never strand the next authorized publication."""

import fcntl
import os
from datetime import UTC, datetime

import pytest
from vonk_agent_protocol import (
    GatewayRouteState,
    UnknownError,
)
from vonk_agent_protocol.route_activation import ActivationMarker
from vonk_control.litellm import render_empty_config
from vonk_control.route_bundle_contract import RouteBundleDocument
from vonk_control.route_runtime import (
    RECIPE_ROUTE_AUTHORITY_ID,
    AtomicRouteBundlePublisher,
    FileSupervisorAcknowledger,
    verify_active_route_bundle,
)


def _activate(publisher, generation=1):
    with publisher._locked() as uncertainty:
        if uncertainty is not None:
            return uncertainty
        return publisher._activate(
            generation=generation,
            state=GatewayRouteState.PUBLISHED,
            authority_id=RECIPE_ROUTE_AUTHORITY_ID,
            plan_digest="a" * 64,
            evidence_set_digest="b" * 64,
            routes=RouteBundleDocument(
                generation=generation,
                routes={},
                schema_version=2,
                state=GatewayRouteState.PUBLISHED,
            )
            .model_dump_json()
            .encode(),
            litellm=render_empty_config(),
        )


@pytest.mark.parametrize("after_replace", [False, True])
def test_activation_uncertainty_releases_lock_and_admits_fresh_publication(
    tmp_path,
    monkeypatch,
    after_replace,
):
    """Catches both stranding the lock and pretending fsync failure had no effect."""
    publisher = AtomicRouteBundlePublisher(tmp_path)
    real_replace = os.replace
    fault = [True]

    def lost_activation(source, target):
        if target == tmp_path / "activation.json" and fault[0]:
            if after_replace:
                real_replace(source, target)
            raise OSError("injected activation interruption")
        return real_replace(source, target)

    monkeypatch.setattr(os, "replace", lost_activation)
    result = _activate(publisher)
    assert isinstance(result, UnknownError)
    current = publisher.inspect()
    if after_replace:
        assert isinstance(current, ActivationMarker)
    else:
        assert isinstance(current, UnknownError)
    descriptor = os.open(tmp_path / ".publication.lock", os.O_RDWR)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(descriptor)
    fault[0] = False
    marker = _activate(publisher, generation=2)
    assert isinstance(marker, ActivationMarker)
    bundle = verify_active_route_bundle(tmp_path)
    assert not isinstance(bundle, UnknownError)
    assert bundle.marker == marker


def test_missing_bundle_is_observed_and_request_led_publication_recovers(tmp_path):
    """Catches refusing an absent activation instead of preparing its requested bundle."""
    observed = verify_active_route_bundle(tmp_path)
    assert isinstance(observed, UnknownError)
    publisher = AtomicRouteBundlePublisher(tmp_path)
    marker = _activate(publisher)
    assert isinstance(marker, ActivationMarker)
    assert publisher.inspect(expected=marker) == marker


def test_absent_ack_ends_at_deadline_without_changing_working_activation(tmp_path):
    """Catches an exception or route withdrawal when the live acknowledgement is unknown."""
    publisher = AtomicRouteBundlePublisher(tmp_path)
    marker = _activate(publisher)
    assert isinstance(marker, ActivationMarker)
    elapsed = [0.0]

    def advance(seconds):
        elapsed[0] += seconds

    acknowledge = FileSupervisorAcknowledger(
        tmp_path / "ack.json",
        clock=lambda: datetime(2026, 10, 8, tzinfo=UTC),
        timeout_seconds=1,
        poll_seconds=0.25,
        monotonic=lambda: elapsed[0],
        sleep=advance,
    )
    observed = acknowledge(marker)
    assert isinstance(observed, UnknownError)
    assert elapsed[0] == 1
    assert publisher.inspect(expected=marker) == marker
    assert isinstance(_activate(publisher, generation=2), ActivationMarker)


def test_contended_publication_returns_without_sleep_and_fresh_request_succeeds(
    tmp_path, monkeypatch
):
    """A competing writer must not park the executor in a lock retry loop."""
    import vonk_control.route_runtime as runtime

    publisher = AtomicRouteBundlePublisher(tmp_path)
    first = _activate(publisher)
    descriptor = os.open(tmp_path / ".publication.lock", os.O_RDWR)

    def parked(_seconds):
        pytest.fail("publication lock contention parked its executor")

    monkeypatch.setattr(runtime.time, "sleep", parked)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = _activate(publisher, generation=2)
        assert isinstance(result, UnknownError)
        assert publisher.inspect() == first
    finally:
        os.close(descriptor)
    fresh = _activate(publisher, generation=2)
    assert isinstance(fresh, ActivationMarker)
    assert publisher.inspect() == fresh


@pytest.mark.parametrize("damage", [b"{", b"\xff", b"{}", b"[]"])
def test_saved_marker_damage_is_unknown_and_next_request_repairs(tmp_path, damage):
    """Syntax and shape damage must not escape accepted-route reconstruction."""
    publisher = AtomicRouteBundlePublisher(tmp_path)
    first = _activate(publisher)
    assert isinstance(first, ActivationMarker)
    (tmp_path / "activation.json").write_bytes(damage)
    assert isinstance(publisher.inspect(), UnknownError)
    assert isinstance(verify_active_route_bundle(tmp_path), UnknownError)
    fresh = _activate(publisher, generation=2)
    assert isinstance(fresh, ActivationMarker)
    bundle = verify_active_route_bundle(tmp_path)
    assert not isinstance(bundle, UnknownError)
    assert bundle.marker == fresh
