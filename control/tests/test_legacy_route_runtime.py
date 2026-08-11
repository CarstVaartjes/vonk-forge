from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from vonk_control.auth import AgentIdentity, AgentSource
from vonk_control.legacy_route_runtime import ProductionRouteManager, RouteRuntimeError
from vonk_control.models import AgentCertificate, AgentNode, Base
from vonk_control.presence import AgentPresenceService, ManagementAddressPolicy

NODE_ID = "spk_" + "0" * 31 + "1"
SECOND_NODE_ID = "spk_" + "0" * 31 + "2"
NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
ROUTES = {
    "deepseek": {
        "node_id": NODE_ID,
        "workload": "deepseek-agent-single",
        "requests_per_minute": 30,
        "tokens_per_minute": 10_000,
    }
}


def _source(node_id: str, address: str) -> AgentSource:
    return AgentSource(
        identity=AgentIdentity(
            node_id,
            f"serial-{node_id}",
            f"fingerprint-{node_id}",
            True,
        ),
        management_address=address,
    )


def _manager(
    tmp_path,
    *,
    probe=lambda _url, _key: None,
    observed_at=NOW,
    clock=lambda: NOW,
    maturity=None,
    presence_addresses=None,
):
    maturity = maturity or {
        "deepseek-agent-dual": "accepted",
        "deepseek-agent-single": "verified",
    }
    presence_addresses = presence_addresses or {NODE_ID: "10.0.0.42"}
    repository = tmp_path / "repository"
    workload_root = repository / "config/workloads"
    workload_root.mkdir(parents=True)
    for workload_id in ("deepseek-agent-dual", "deepseek-agent-single"):
        (workload_root / f"{workload_id}.toml").write_text(
            f'id = "{workload_id}"\n[endpoint]\nhost = "127.0.0.1"\nport = 8888\n'
        )
    (repository / "config/hermes-agent-policy.toml").write_text(
        "schema_version = 1\n"
        'alias = "hermes-agent"\n'
        "local_only = true\n\n"
        "[[candidates]]\n"
        'workload = "deepseek-agent-dual"\n'
        "priority = 1\n"
        'minimum_maturity = "accepted"\n\n'
        "[[candidates]]\n"
        'workload = "deepseek-agent-single"\n'
        "priority = 2\n"
        'minimum_maturity = "accepted"\n'
    )
    report = repository / "inventory/reports/model-definitions.json"
    report.parent.mkdir(parents=True)
    report.write_text(json.dumps({
        "definitions": [
            {"id": workload_id, "maturity": state}
            for workload_id, state in maturity.items()
        ]
    }))
    fleet = repository / "inventory/fleet.toml"
    fleet.parent.mkdir(parents=True, exist_ok=True)
    fleet.write_text(
        "".join(
            f'[nodes."{node_id}"]\nlifecycle = "ready"\n'
            for node_id in presence_addresses
        )
    )
    engine = create_engine(f"sqlite:///{tmp_path / 'routes.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    with sessions.begin() as session:
        session.add_all(
            AgentNode(node_id=node_id, state="active", capabilities=[])
            for node_id in presence_addresses
        )
        session.add_all(
            AgentCertificate(
                serial=f"serial-{node_id}",
                node_id=node_id,
                fingerprint=f"fingerprint-{node_id}",
                state="active",
                generation=1,
                not_before=observed_at - timedelta(minutes=1),
                not_after=NOW + timedelta(days=1),
            )
            for node_id in presence_addresses
        )
    policy = ManagementAddressPolicy.parse("10.0.0.0/24")
    writer = AgentPresenceService(sessions, policy, clock=lambda: observed_at)
    for node_id, address in presence_addresses.items():
        writer.observe(_source(node_id, address))
    presence = AgentPresenceService(sessions, policy, clock=clock)
    live = tmp_path / "live/config.yaml"
    manager = ProductionRouteManager(
        repository,
        state_root=tmp_path / "state",
        live_config=live,
        presence=presence,
        management_policy=policy,
        upstream_key="upstream-test-key",
        probe=probe,
        clock=clock,
        maximum_age_seconds=150,
        refresh_interval_seconds=60,
        repository_reader=lambda _commit, path: (repository / path).read_bytes(),
    )
    return manager, live, presence


def _v2_route(workload: str, node_id: str, *, port: int = 9999):
    quota = {"requests_per_minute": 30, "tokens_per_minute": 10_000}
    return {
        "workload_id": workload,
        "nodes": [node_id],
        "entrypoint_node_id": node_id,
        "scheme": "http",
        "port": port,
        "path": "/v1",
        "quota": quota,
        "quota_digest": hashlib.sha256(
            json.dumps(quota, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    }


def _hermes_models(live):
    return [
        item for item in json.loads(live.read_bytes())["model_list"]
        if item["model_name"] == "hermes-agent"
    ]


def test_successful_reconciliation_publishes_probed_presence_route(tmp_path) -> None:
    probes = []
    manager, live, _presence = _manager(
        tmp_path,
        probe=lambda url, key: probes.append((url, key)),
    )
    manager.withdraw((NODE_ID,))
    assert json.loads(live.read_bytes())["model_list"] == []

    result = manager.publish(
        commit="a" * 40,
        profile="agent-single",
        targets=(NODE_ID,),
        routes=ROUTES,
    )

    assert probes == [("http://10.0.0.42:8888/v1/models", "upstream-test-key")]
    config = json.loads(live.read_bytes())
    assert config["model_list"][0]["model_name"] == "deepseek"
    assert config["model_list"][0]["litellm_params"]["api_base"] == "http://10.0.0.42:8888/v1"
    assert b"upstream-test-key" not in live.read_bytes()
    assert result.route_state.health_timestamp == NOW.isoformat()
    lease = json.loads((live.parent / "lease.json").read_bytes())
    assert lease["config_sha256"] == hashlib.sha256(live.read_bytes()).hexdigest()
    assert lease["issued_at"] == NOW.isoformat()
    assert lease["expires_at"] == (NOW + timedelta(seconds=150)).isoformat()


def test_probe_failure_keeps_litellm_withdrawn(tmp_path) -> None:
    def fail(_url, _key):
        raise OSError("refused")

    manager, live, _presence = _manager(tmp_path, probe=fail)
    manager.withdraw((NODE_ID,))

    with pytest.raises(RouteRuntimeError, match="probe"):
        manager.publish(
            commit="a" * 40,
            profile="agent-single",
            targets=(NODE_ID,),
            routes=ROUTES,
        )

    assert json.loads(live.read_bytes())["model_list"] == []


def test_stale_presence_never_reaches_probe_or_live_routes(tmp_path) -> None:
    manager, live, _presence = _manager(
        tmp_path,
        observed_at=NOW - timedelta(seconds=151),
        probe=lambda _url, _key: pytest.fail("must not probe stale address"),
    )
    manager.withdraw((NODE_ID,))

    with pytest.raises(RouteRuntimeError, match="stale"):
        manager.publish(
            commit="a" * 40,
            profile="agent-single",
            targets=(NODE_ID,),
            routes=ROUTES,
        )

    assert json.loads(live.read_bytes())["model_list"] == []


def test_route_port_must_come_from_repository_workload(tmp_path) -> None:
    manager, _live, _presence = _manager(tmp_path)
    forged = {"deepseek": dict(ROUTES["deepseek"], port=9999)}

    with pytest.raises(RouteRuntimeError, match="fields"):
        manager.publish(
            commit="a" * 40,
            profile="agent-single",
            targets=(NODE_ID,),
            routes=forged,
        )


def test_refresh_republishes_a_changed_dhcp_observation(tmp_path) -> None:
    current = [NOW]
    probes = []
    manager, live, presence = _manager(
        tmp_path,
        clock=lambda: current[0],
        probe=lambda url, _key: probes.append(url),
    )
    manager.withdraw((NODE_ID,))
    manager.publish(
        commit="a" * 40,
        profile="agent-single",
        targets=(NODE_ID,),
        routes=ROUTES,
    )
    current[0] = NOW + timedelta(seconds=60)
    presence.observe(_source(NODE_ID, "10.0.0.43"))

    assert manager.refresh_if_due(lambda: "a" * 40) is True
    config = json.loads(live.read_bytes())
    assert config["model_list"][0]["litellm_params"]["api_base"] == "http://10.0.0.43:8888/v1"
    assert probes[-1] == "http://10.0.0.43:8888/v1/models"


def test_changed_dhcp_route_is_withdrawn_before_replacement_probe(tmp_path) -> None:
    current = [NOW]
    live_holder = []

    def probe(url, _key):
        if url.startswith("http://10.0.0.43"):
            assert json.loads(live_holder[0].read_bytes())["model_list"] == []

    manager, live, presence = _manager(
        tmp_path,
        clock=lambda: current[0],
        probe=probe,
    )
    live_holder.append(live)
    manager.publish(
        commit="a" * 40,
        profile="agent-single",
        targets=(NODE_ID,),
        routes=ROUTES,
    )
    current[0] = NOW + timedelta(seconds=60)
    presence.observe(_source(NODE_ID, "10.0.0.43"))

    assert manager.refresh_if_due(lambda: "a" * 40) is True


def test_route_target_must_be_ready_in_accepted_repository_fleet(tmp_path) -> None:
    manager, live, _presence = _manager(
        tmp_path,
        probe=lambda _url, _key: pytest.fail("unaccepted node must not be probed"),
    )
    fleet = tmp_path / "repository/inventory/fleet.toml"
    fleet.write_text(f'[nodes."{NODE_ID}"]\nlifecycle = "quarantined"\n')

    with pytest.raises(RouteRuntimeError, match="accepted fleet"):
        manager.publish(
            commit="a" * 40,
            profile="agent-single",
            targets=(NODE_ID,),
            routes=ROUTES,
        )

    assert json.loads(live.read_bytes())["model_list"] == []


def test_desired_state_is_durable_before_any_probe_or_activation(tmp_path, monkeypatch) -> None:
    manager, live, _presence = _manager(
        tmp_path,
        probe=lambda _url, _key: pytest.fail("must not probe before desired state is durable"),
    )

    def fail(_content):
        raise OSError("disk full")

    monkeypatch.setattr(manager._desired, "write", fail)
    with pytest.raises(RouteRuntimeError, match="disk full"):
        manager.publish(
            commit="a" * 40,
            profile="agent-single",
            targets=(NODE_ID,),
            routes=ROUTES,
        )

    assert json.loads(live.read_bytes())["model_list"] == []


def test_refresh_withdraws_routes_when_presence_expires(tmp_path) -> None:
    current = [NOW]
    manager, live, _presence = _manager(tmp_path, clock=lambda: current[0])
    manager.withdraw((NODE_ID,))
    manager.publish(
        commit="a" * 40,
        profile="agent-single",
        targets=(NODE_ID,),
        routes=ROUTES,
    )
    current[0] = NOW + timedelta(seconds=151)

    assert manager.refresh_if_due(lambda: "a" * 40) is False
    assert json.loads(live.read_bytes())["model_list"] == []


def test_refresh_is_bounded_and_skips_before_interval(tmp_path) -> None:
    current = [NOW]
    manager, _live, _presence = _manager(tmp_path, clock=lambda: current[0])
    manager.withdraw((NODE_ID,))
    manager.publish(
        commit="a" * 40,
        profile="agent-single",
        targets=(NODE_ID,),
        routes=ROUTES,
    )

    assert manager.refresh_if_due(lambda: pytest.fail("must not resolve commit")) is False


def test_refresh_withdraws_when_commit_loses_eligibility(tmp_path) -> None:
    current = [NOW]
    manager, live, _presence = _manager(tmp_path, clock=lambda: current[0])
    manager.withdraw((NODE_ID,))
    manager.publish(
        commit="a" * 40,
        profile="agent-single",
        targets=(NODE_ID,),
        routes=ROUTES,
    )
    current[0] = NOW + timedelta(seconds=60)

    assert manager.refresh_if_due(lambda: "a" * 40, eligible=lambda _commit: False) is False
    assert json.loads(live.read_bytes())["model_list"] == []


def test_dual_candidate_outranks_simultaneously_running_single_candidate(tmp_path) -> None:
    manager, live, _presence = _manager(
        tmp_path,
        maturity={
            "deepseek-agent-dual": "accepted",
            "deepseek-agent-single": "accepted",
        },
        presence_addresses={NODE_ID: "10.0.0.42", SECOND_NODE_ID: "10.0.0.43"},
    )
    manager.publish(
        commit="a" * 40,
        profile="mixed",
        targets=(NODE_ID, SECOND_NODE_ID),
        routes={
            "dual": _v2_route("deepseek-agent-dual", NODE_ID),
            "single": _v2_route("deepseek-agent-single", SECOND_NODE_ID),
        },
    )

    assert [item["litellm_params"]["model"] for item in _hermes_models(live)] == [
        "openai/deepseek-agent-dual",
        "openai/deepseek-agent-single",
    ]
    assert [item["litellm_params"]["order"] for item in _hermes_models(live)] == [1, 2]


def test_verified_single_candidate_is_not_added_to_hermes_group(tmp_path) -> None:
    manager, live, _presence = _manager(tmp_path)
    manager.publish(
        commit="a" * 40,
        profile="agent-single",
        targets=(NODE_ID,),
        routes={"deepseek": _v2_route("deepseek-agent-single", NODE_ID)},
    )

    assert _hermes_models(live) == []
    assert json.loads(live.read_bytes())["model_list"][0]["model_name"] == "deepseek"


def test_mixed_profile_can_publish_an_accepted_single_candidate(tmp_path) -> None:
    manager, live, _presence = _manager(
        tmp_path,
        maturity={
            "deepseek-agent-dual": "accepted",
            "deepseek-agent-single": "accepted",
        },
    )
    manager.publish(
        commit="a" * 40,
        profile="mixed",
        targets=(NODE_ID,),
        routes={"deepseek": _v2_route("deepseek-agent-single", NODE_ID)},
    )

    assert [item["litellm_params"]["model"] for item in _hermes_models(live)] == [
        "openai/deepseek-agent-single"
    ]


def test_failed_primary_probe_leaves_only_an_eligible_secondary(tmp_path) -> None:
    def probe(url, _key):
        if url.startswith("http://10.0.0.42"):
            raise OSError("primary refused")

    manager, live, _presence = _manager(
        tmp_path,
        probe=probe,
        maturity={
            "deepseek-agent-dual": "accepted",
            "deepseek-agent-single": "accepted",
        },
        presence_addresses={NODE_ID: "10.0.0.42", SECOND_NODE_ID: "10.0.0.43"},
    )
    routes = {
        "dual": _v2_route("deepseek-agent-dual", NODE_ID),
        "single": _v2_route("deepseek-agent-single", SECOND_NODE_ID),
    }
    with pytest.raises(RouteRuntimeError, match="probe"):
        manager.publish(
            commit="a" * 40,
            profile="mixed",
            targets=(NODE_ID, SECOND_NODE_ID),
            routes=routes,
        )
    assert json.loads(live.read_bytes())["model_list"] == []

    manager.publish(
        commit="a" * 40,
        profile="mixed",
        targets=(NODE_ID, SECOND_NODE_ID),
        routes=routes,
    )
    assert [item["litellm_params"]["model"] for item in _hermes_models(live)] == [
        "openai/deepseek-agent-single"
    ]


def test_no_eligible_candidate_omits_hermes_group_but_keeps_other_routes(tmp_path) -> None:
    manager, live, _presence = _manager(tmp_path)
    manager.publish(
        commit="a" * 40,
        profile="agent-single",
        targets=(NODE_ID,),
        routes={"deepseek": _v2_route("deepseek-agent-single", NODE_ID)},
    )

    assert [item["model_name"] for item in json.loads(live.read_bytes())["model_list"]] == [
        "deepseek"
    ]


def test_v2_reconciliation_route_shape_is_normalized_without_trusting_an_address(tmp_path) -> None:
    manager, live, _presence = _manager(tmp_path)
    manager.publish(
        commit="a" * 40,
        profile="agent-single",
        targets=(NODE_ID,),
        routes={"deepseek": _v2_route("deepseek-agent-single", NODE_ID, port=9999)},
    )

    model = json.loads(live.read_bytes())["model_list"][0]
    assert model["litellm_params"]["api_base"] == "http://10.0.0.42:8888/v1"
