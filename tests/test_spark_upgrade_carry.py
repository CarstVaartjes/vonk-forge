"""The upgrade-carry lane's verdict and its first-release behaviour."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from tests.acceptance import spark_upgrade_carry as carry
from tests.acceptance.test_spark_lifecycle import LifecycleError


def _lane(probes: list[carry.ProbeResult]) -> carry.UpgradeCarryLifecycle:
    lane = object.__new__(carry.UpgradeCarryLifecycle)
    lane.evidence = carry.CarryEvidence(probes=probes)
    return lane


def _probe(phase: str, *, ok: bool = True, route: str = "published"):
    return carry.ProbeResult(
        at=0.0,
        phase=phase,
        models_listed=ok,
        inference_ok=ok,
        route_state=route,
        detail=None if ok else "gateway does not list the carried model",
    )


def test_a_withdrawn_route_after_the_agent_upgrade_fails_and_names_the_phase(
    capsys,
):
    # What release d30de9199 did: the upgraded agent stopped reporting the
    # run the previous agent started, and the Controller withdrew its route.
    lane = _lane(
        [
            _probe("baseline-serving"),
            _probe("agent-upgrade"),
            _probe("agent-settled"),
            _probe("agent-settled", ok=False, route="withdrawn"),
        ]
    )
    with pytest.raises(LifecycleError):
        lane._judge()
    # The full probe evidence goes to the log and the report.
    assert lane.failure_evidence is not None
    _lane([_probe("agent-settled")])._judge()


def test_the_gateway_restart_during_the_controller_recreate_is_tolerated():
    lane = _lane(
        [
            _probe("baseline-serving"),
            _probe("controller-redeploy", ok=False, route="absent"),
            _probe("controller-redeploy", ok=False, route="absent"),
            _probe("controller-settled"),
            _probe("agent-settled"),
        ]
    )
    lane._judge()


def test_one_missed_probe_is_tolerated_but_two_in_a_row_are_not():
    _lane(
        [
            _probe("agent-settled"),
            _probe("agent-settled", ok=False),
            _probe("agent-settled"),
        ]
    )._judge()
    with pytest.raises(LifecycleError):
        _lane(
            [
                _probe("agent-upgrade", ok=False),
                _probe("agent-upgrade", ok=False),
                _probe("agent-settled"),
            ]
        )._judge()


def test_the_first_release_is_skipped_visibly(tmp_path: Path, monkeypatch, capsys):
    output = tmp_path / "report.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "spark_upgrade_carry",
            "--channel",
            "dev",
            "--candidate-generation",
            "a" * 64,
            "--run-id",
            "1",
            "--output",
            str(output),
        ],
    )
    assert carry.main() == 0
    assert (
        "::notice title=Upgrade-carry acceptance skipped::" in capsys.readouterr().out
    )
    report = json.loads(output.read_text())
    assert report["status"] == "skipped" and "no previous promoted" in report["reason"]


CLEANUP_ID = "11111111-1111-4111-8111-111111111111"
STOP_ID = "22222222-2222-4222-8222-222222222222"
REMOVE_ID = "33333333-3333-4333-8333-333333333333"
RUN_ID = "44444444-4444-4444-8444-444444444444"
INSTALLATION_ID = "55555555-5555-4555-8555-555555555555"


def _serving_receipt(run_id: str):
    from vonk_agent_protocol.agent_words import ProfileReportedPhase
    from vonk_agent_protocol.state_machines import RouteState, RunState
    from vonk_control.run_switch_contract import RunSwitchFinalVerifyResult

    return RunSwitchFinalVerifyResult.model_validate_json(
        json.dumps(
            {
                "phase": ProfileReportedPhase.FINAL_VERIFY,
                "final_verified": True,
                "run_id": run_id,
                "state": RunState.RUNNING,
                "route_state": RouteState.PUBLISHED,
                "healthy": True,
                "ranks": [],
            }
        )
    ).model_dump(mode="json")


def _serving_fleet(installation_id: str, run_id: str):
    from datetime import UTC, datetime

    from vonk_agent_protocol.state_machines import (
        CertificateState,
        RouteState,
        RunState,
    )
    from vonk_control.fleet_projection.common import (
        CapacityReservations,
        FleetNode,
        FleetSnapshot,
        NodeConnection,
        RunPresence,
    )

    node_id = "spk_0123456789abcdef0123456789abcdef"
    run = RunPresence(
        run_id=run_id,
        installation_id=installation_id,
        recipe_id="recipe",
        recipe_revision_id="revision",
        title="canary",
        alias="canary",
        expected_rank_count=1,
        present_ranks=[0],
        member_node_ids=[node_id],
        rank=0,
        role="leader",
        run_state=RunState.RUNNING,
        route_state=RouteState.PUBLISHED,
        rank_state=RunState.RUNNING,
        rank_age_seconds=0,
        rank_fresh=True,
        group_state="healthy",
        healthy=True,
    )
    node = FleetNode(
        id=node_id,
        display_name="Spark",
        hostname="spark",
        lifecycle="active",
        labels=None,
        connection=NodeConnection(
            agent_state="active",
            certificate_state=CertificateState.VALID,
            online_state="online",
            offline_reason=None,
            last_seen_at=None,
            last_seen_age_seconds=None,
        ),
        inventory=None,
        telemetry=None,
        installed=[],
        loaded=[run],
        warnings=[],
        reservations=CapacityReservations(
            disk_bytes=0,
            unified_memory_bytes=0,
            host_memory_bytes=0,
            gpu_memory_bytes=0,
            port_count=0,
        ),
    )
    return FleetSnapshot(
        authority_revision="a" * 64,
        event_cursor=0,
        generated_at=datetime.now(UTC),
        nodes=[node],
    ).model_dump(mode="json")


@pytest.mark.parametrize(
    "installs", [[], [INSTALLATION_ID], [INSTALLATION_ID, CLEANUP_ID]]
)
def test_successor_uses_verified_serving_content_even_when_install_is_reused(
    monkeypatch, installs
):
    """Catches receipt-count/provenance guessing on reused and accumulated installs."""
    lane = object.__new__(carry.UpgradeCarryLifecycle)
    results = [
        {"phase": "prepare", "subphase": "runtime-install", "installation_id": value}
        for value in installs
    ] + [_serving_receipt(CLEANUP_ID), _serving_receipt(RUN_ID)]
    monkeypatch.setattr(
        lane, "_fleet_snapshot", lambda: _serving_fleet(INSTALLATION_ID, RUN_ID)
    )
    assert lane._serving_identity(results, application_id=REMOVE_ID) == (
        INSTALLATION_ID,
        RUN_ID,
    )


def test_serving_identity_reobserves_unknown_and_ends_without_poisoning_fresh_load(
    monkeypatch,
):
    """Catches treating stale Fleet/receipt evidence as a permanent refusal."""
    import itertools

    from tests.acceptance import test_spark_lifecycle as lifecycle

    lane = object.__new__(carry.UpgradeCarryLifecycle)
    receipt = _serving_receipt(RUN_ID)

    class Control:
        def request(self, method, path):
            return 200, {
                "progress": {
                    "step_results": {
                        "child": {
                            "result": {"run_switch": {"phase_results": [receipt]}}
                        }
                    }
                }
            }

    monkeypatch.setattr(lane, "control", Control(), raising=False)
    monkeypatch.setattr(lifecycle, "_CANARY_ROUTE_SECONDS", 1)
    ticks = itertools.count(step=0.1)
    monkeypatch.setattr(lifecycle.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(lifecycle.time, "sleep", lambda _: None)
    stale = _serving_fleet(INSTALLATION_ID, CLEANUP_ID)
    monkeypatch.setattr(lane, "_fleet_snapshot", lambda: stale)
    with pytest.raises(LifecycleError):
        lane._serving_identity([receipt], application_id=REMOVE_ID)
    # An ended observation leaves no gate; an unreadable receipt is fetched again.
    monkeypatch.setattr(
        lane, "_fleet_snapshot", lambda: _serving_fleet(INSTALLATION_ID, RUN_ID)
    )
    assert lane._serving_identity([], application_id=REMOVE_ID) == (
        INSTALLATION_ID,
        RUN_ID,
    )


def _scenario_lane() -> carry.UpgradeCarryLifecycle:
    import threading

    lane = _lane([])
    lane._lock = threading.Lock()
    lane._phase = "baseline-install"
    return lane


def _failing() -> dict[str, object]:
    raise LifecycleError("the second install was refused")


def test_an_observed_phase_that_fails_is_reported_and_does_not_fail_the_lane(
    monkeypatch, capsys
):
    monkeypatch.setattr(carry, "OBSERVED_PHASES", frozenset({"new-phase"}))
    lane = _scenario_lane()
    lane._run_scenario("new-phase", _failing)
    assert lane.evidence.observed == [
        {
            "phase": "new-phase",
            "status": "failed",
            "error": "LifecycleError: the second install was refused",
        }
    ]
    assert "::warning title=Observed upgrade-carry phase new-phase failed" in (
        capsys.readouterr().out
    )


def test_an_observed_phase_that_crashes_on_a_surprise_is_still_only_observed(
    monkeypatch,
):
    monkeypatch.setattr(carry, "OBSERVED_PHASES", frozenset({"new-phase"}))
    lane = _scenario_lane()
    lane._run_scenario("new-phase", lambda: {}["missing"])
    assert lane.evidence.observed[0]["status"] == "failed"


def test_a_phase_taken_out_of_the_observed_set_gates_the_lane(monkeypatch):
    # Promoting a phase to gating is deleting its name from OBSERVED_PHASES.
    monkeypatch.setattr(carry, "OBSERVED_PHASES", frozenset())
    lane = _scenario_lane()
    with pytest.raises(LifecycleError):
        lane._run_scenario("new-phase", _failing)
    assert lane.evidence.observed == []


def test_an_observed_phase_that_passes_leaves_its_evidence_in_the_report(
    monkeypatch,
):
    monkeypatch.setattr(carry, "OBSERVED_PHASES", frozenset({"new-phase"}))
    lane = _scenario_lane()
    lane._run_scenario("new-phase", lambda: {"image_digest": "sha256:" + "a" * 64})
    (record,) = lane.evidence.observed
    assert record["status"] == "passed"
    assert str(record["image_digest"]).startswith("sha256:")


HISTORICAL_CLEANUP_SOURCE = "3d0bc507c4917f7d41f0938d29c7a64b26b8d0ea"


def _historical_cleanup() -> tuple[carry.UpgradeCarryLifecycle, dict]:
    """Authored receipt in the actual identical baseline/candidate schema.

    This is a schema/semantic boundary proof, not a captured physical receipt;
    the real hosted carry lane must still complete its producer effects.
    """
    from tests.acceptance.controller_contract import ControllerContract

    fixture = json.loads(
        (
            Path(__file__).parent / "fixtures/historical-profile-cleanup-contracts.json"
        ).read_text(encoding="utf-8")
    )
    assert {entry["source_sha"] for entry in fixture["provenance"]} == {
        HISTORICAL_CLEANUP_SOURCE,
        "f8a65ee9eeb6ab82e3e31004fcc159140f5b2b44",
    }
    contract = ControllerContract(
        fixture["openapi"], label="published historical source"
    )
    release = carry.ReleaseInput(
        generation="a" * 64,
        source_sha=HISTORICAL_CLEANUP_SOURCE,
        caddyfile="",
        release=Path("release.json"),
        signature=Path("release.sig"),
        overlay=Path("overlay.yml"),
        version="1.0.0",
        package_version="1.0.0",
        contract=contract,
        # Both verified historical sources publish these four image roles;
        # their stock step-ca service is not the current signed CA image role.
        compose_image_roles={
            "api": "control-api",
            "worker": "control-worker",
            "hermes": "hermes-agent",
            "litellm": "litellm",
        },
    )
    lane = _lane([])
    lane.baseline = release
    lane.candidate = release
    lane.controller_generation = release.generation
    children = [
        {
            "operation_id": STOP_ID,
            "kind": "stop",
            "state": "succeeded",
            "result": {
                "run_switch_operation_id": STOP_ID,
                "run_switch": {"phase_results": [{"phase": "stop", "run_id": RUN_ID}]},
            },
        },
        {
            "operation_id": REMOVE_ID,
            "kind": "cleanup",
            "state": "succeeded",
            "result": {
                "run_switch_operation_id": REMOVE_ID,
                "run_switch": {
                    "phase_results": [
                        {"phase": "uninstall", "installation_id": INSTALLATION_ID},
                        {
                            "phase": "final_verify",
                            "installation_id": INSTALLATION_ID,
                            "final_verified": True,
                            "removed": True,
                            "active_runs": 0,
                            "installation_state": "uninstalled",
                        },
                    ]
                },
            },
        },
    ]
    result = {"children": children, "assignment_ids": []}
    application = {
        "id": CLEANUP_ID,
        "request_key": CLEANUP_ID,
        "profile_id": CLEANUP_ID,
        "profile_digest": "a" * 64,
        "plan_digest": "b" * 64,
        "state": "succeeded",
        "current_step": 1,
        "total_steps": 1,
        "current_operation_id": None,
        "status_reason": None,
        "created_at": "2026-10-07T00:00:00Z",
        "updated_at": "2026-10-07T00:00:01Z",
        "result": {"changed": True, "completed_steps": 1},
        "progress": {
            "step_results": {
                "0": {"operation_id": CLEANUP_ID, "kind": "switch", "result": result}
            },
            "switch_adapter": {
                "child_id": CLEANUP_ID,
                "scope_node_ids": [],
                "assignment_ids": [],
                "assignments": [],
                "queue": [
                    {"kind": "stop", "id": RUN_ID},
                    {"kind": "cleanup", "id": INSTALLATION_ID},
                ],
                "position": 2,
                "actor": "acceptance",
                "request_id": CLEANUP_ID,
                "active_operation_id": None,
                "active_kind": None,
                "children": children,
                "state": "succeeded",
                "result": result,
            },
        },
    }
    return lane, application


def test_cleanup_uses_the_receipt_producers_historical_schema() -> None:
    from tests.acceptance.test_spark_lifecycle import (
        _validate_canary_cleanup_application,
    )

    lane, application = _historical_cleanup()
    with pytest.raises(LifecycleError):
        _validate_canary_cleanup_application(
            application, installation_ids=[INSTALLATION_ID], run_id=RUN_ID
        )
    lane._validate_cleanup_application(
        application, installation_ids=[INSTALLATION_ID], run_id=RUN_ID
    )


@pytest.mark.parametrize(
    "damage", ["required", "foreign-child", "foreign-run", "unverified"]
)
def test_historical_schema_does_not_weaken_cleanup_evidence(damage: str) -> None:
    from tests.acceptance.controller_contract import ContractSkew

    lane, application = _historical_cleanup()
    children = application["progress"]["switch_adapter"]["result"]["children"]
    if damage == "required":
        del application["request_key"]
    elif damage == "foreign-child":
        children[0]["result"]["run_switch_operation_id"] = CLEANUP_ID
    elif damage == "foreign-run":
        children[0]["result"]["run_switch"]["phase_results"][0]["run_id"] = CLEANUP_ID
    else:
        children[1]["result"]["run_switch"]["phase_results"][1]["final_verified"] = (
            False
        )
    with pytest.raises((LifecycleError, ContractSkew)):
        lane._validate_cleanup_application(
            application, installation_ids=[INSTALLATION_ID], run_id=RUN_ID
        )


def test_a_superseded_load_is_followed_to_its_automatic_retry(monkeypatch):
    """An attempt superseded by the platform's own retry is not a lane failure."""
    lane = object.__new__(carry.UpgradeCarryLifecycle)
    replies = {
        "first": {
            "id": "first",
            "state": "superseded",
            "successor_application_id": "retry",
        },
        "retry": {"id": "retry", "state": "succeeded"},
    }

    class Control:
        def request(self, method, path):
            return 200, dict(replies[path.rsplit("/", 1)[-1]])

    monkeypatch.setattr(lane, "control", Control(), raising=False)
    monkeypatch.setattr(carry.time, "sleep", lambda _seconds: None)
    result = lane._await_profile_application(
        dict(replies["first"]), label="editorial successor profile load", node_id="n"
    )
    assert result["id"] == "retry" and result["state"] == "succeeded"


def test_a_superseded_load_without_a_successor_still_fails(monkeypatch):
    lane = object.__new__(carry.UpgradeCarryLifecycle)
    monkeypatch.setattr(
        lane,
        "control",
        type("Control", (), {"request": lambda self, m, p: (200, {})})(),
        raising=False,
    )
    monkeypatch.setattr(carry.time, "sleep", lambda _seconds: None)
    with pytest.raises(LifecycleError):
        lane._await_profile_application(
            {"id": "only", "state": "superseded"}, label="load", node_id="n"
        )


def test_acceptance_scripts_define_everything_before_their_entry_point():
    """A helper defined after the __main__ block does not exist when run as a script."""
    import ast

    root = Path(__file__).resolve().parent / "acceptance"
    late: list[str] = []
    for script in sorted(root.glob("*.py")):
        body = ast.parse(script.read_text()).body
        guards = [
            index
            for index, node in enumerate(body)
            if isinstance(node, ast.If) and "__main__" in ast.unparse(node.test)
        ]
        if guards and guards[-1] != len(body) - 1:
            late.append(script.name)
    assert late == []
