from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from vonk_control.presence import ManagementAddressPolicy
from vonk_control.routes import (
    RouteCandidate,
    RouteEndpoint,
    RouteEndpointPolicy,
    RoutePublisher,
    RouteValidationError,
)

NODE_ID = "spk_" + "0" * 31 + "1"
OTHER_NODE_ID = "spk_" + "0" * 31 + "2"
NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)


def _endpoint(
    address: str = "10.0.0.42",
    *,
    node_id: str = NODE_ID,
    port: int = 8888,
    scheme: str = "http",
    observed_at: datetime = NOW,
) -> RouteEndpoint:
    return RouteEndpoint(node_id, address, port, scheme, observed_at)


def _candidate(
    endpoint: RouteEndpoint | None = None,
    aliases: tuple[str, ...] = ("deepseek", "reasoning"),
    *,
    health_timestamp: datetime = NOW,
) -> RouteCandidate:
    selected = endpoint or _endpoint()
    return RouteCandidate(
        authority_revision="a" * 64,
        profile="agent",
        workload="deepseek",
        node_ids=(NODE_ID,),
        aliases={alias: selected for alias in aliases},
        health_timestamp=health_timestamp,
    )


def _policy() -> RouteEndpointPolicy:
    return RouteEndpointPolicy(
        management=ManagementAddressPolicy.parse(
            "10.0.0.0/24,fd00:10::/64",
            forbidden_cidrs="10.0.0.240/28",
        ),
        allowed_ports=frozenset({8000, 8888}),
        maximum_age_seconds=150,
        clock=lambda: NOW,
    )


def _publisher(tmp_path: Path, *, validate=lambda _: True, apply=lambda _: None) -> RoutePublisher:
    return RoutePublisher(
        tmp_path,
        endpoint_policy=_policy(),
        validate=validate,
        apply=apply,
    )


def test_publish_renders_validated_structured_endpoint(tmp_path: Path) -> None:
    applied: list[bytes] = []
    publisher = _publisher(
        tmp_path,
        validate=lambda content: b"reasoning" in content,
        apply=applied.append,
    )

    state = publisher.publish(_candidate())

    assert state.generation == 1
    assert state.aliases == {
        "deepseek": "http://10.0.0.42:8888/v1",
        "reasoning": "http://10.0.0.42:8888/v1",
    }
    assert publisher.visible_aliases() == {"deepseek", "reasoning"}
    assert len(applied) == 1


def test_ipv6_upstream_is_rendered_with_brackets(tmp_path: Path) -> None:
    state = _publisher(tmp_path).publish(_candidate(_endpoint("fd00:10::42")))
    assert state.aliases["deepseek"] == "http://[fd00:10::42]:8888/v1"


@pytest.mark.parametrize(
    ("endpoint", "message"),
    (
        (_endpoint(node_id=OTHER_NODE_ID), "node"),
        (_endpoint(observed_at=NOW - timedelta(seconds=151)), "stale"),
        (_endpoint(observed_at=NOW + timedelta(seconds=1)), "future"),
        (_endpoint(scheme="https"), "scheme"),
        (_endpoint(port=9999), "port"),
        (_endpoint("user@10.0.0.42"), "IP literal"),
        (_endpoint("node-a.internal"), "IP literal"),
        (_endpoint("10.0.0.241"), "forbidden"),
        (_endpoint("10.0.1.42"), "outside"),
    ),
)
def test_endpoint_policy_rejects_untrusted_routes(
    tmp_path: Path,
    endpoint: RouteEndpoint,
    message: str,
) -> None:
    publisher = _publisher(tmp_path)

    with pytest.raises(RouteValidationError, match=message):
        publisher.publish(_candidate(endpoint))


@pytest.mark.parametrize(
    ("health_timestamp", "message"),
    (
        (NOW - timedelta(seconds=151), "stale"),
        (NOW + timedelta(seconds=1), "future"),
        (NOW.replace(tzinfo=None), "timezone-aware"),
    ),
)
def test_route_health_must_be_current_for_the_published_generation(
    tmp_path: Path,
    health_timestamp: datetime,
    message: str,
) -> None:
    with pytest.raises(RouteValidationError, match=message):
        _publisher(tmp_path).publish(
            _candidate(
                _endpoint(observed_at=NOW - timedelta(seconds=10)),
                health_timestamp=health_timestamp,
            )
        )


def test_route_health_must_follow_the_endpoint_observation(tmp_path: Path) -> None:
    with pytest.raises(RouteValidationError, match="predates"):
        _publisher(tmp_path).publish(
            _candidate(
                _endpoint(observed_at=NOW),
                health_timestamp=NOW - timedelta(seconds=1),
            )
        )


def test_invalid_candidate_keeps_explicit_maintenance_routes(tmp_path: Path) -> None:
    applied: list[bytes] = []
    publisher = _publisher(tmp_path, apply=applied.append)
    publisher.maintenance((NODE_ID,), "switch")

    with pytest.raises(RouteValidationError, match="outside"):
        publisher.publish(_candidate(_endpoint("10.0.1.42")))

    assert publisher.snapshot().state == "maintenance"
    assert publisher.visible_aliases() == set()
    assert len(applied) == 1


def test_address_change_transitions_to_maintenance_before_replacement_validation(
    tmp_path: Path,
) -> None:
    applied: list[bytes] = []

    def validate(content: bytes) -> bool:
        return b"10.0.0.43" not in content

    publisher = _publisher(tmp_path, validate=validate, apply=applied.append)
    first = publisher.publish(_candidate(_endpoint("10.0.0.42"), ("deepseek",)))

    with pytest.raises(RouteValidationError, match="configuration validation"):
        publisher.transition(_candidate(_endpoint("10.0.0.43"), ("deepseek",)))

    assert first.state == "published"
    assert publisher.snapshot().state == "maintenance"
    assert publisher.snapshot().aliases == {}
    assert publisher.visible_aliases() == set()
    assert len(applied) == 2
    assert b"10.0.0.42" in applied[0]
    assert b"10.0.0.42" not in applied[1]


def test_validator_or_apply_failure_keeps_previous_generation(tmp_path: Path) -> None:
    fail = False

    def apply(_content: bytes) -> None:
        if fail:
            raise RuntimeError("LiteLLM rejected generation")

    publisher = _publisher(tmp_path, apply=apply)
    first = publisher.publish(_candidate(aliases=("deepseek",)))
    fail = True
    with pytest.raises(RouteValidationError, match="apply"):
        publisher.publish(_candidate(aliases=("reasoning",)))
    assert publisher.snapshot() == first
    assert publisher.visible_aliases() == {"deepseek"}


def test_route_state_rejects_symlink_root(tmp_path: Path) -> None:
    actual = tmp_path / "actual"
    actual.mkdir()
    link = tmp_path / "routes"
    link.symlink_to(actual)
    with pytest.raises(RouteValidationError, match="symlink"):
        RoutePublisher(
            link,
            endpoint_policy=_policy(),
            validate=lambda _: True,
            apply=lambda _: None,
        )
