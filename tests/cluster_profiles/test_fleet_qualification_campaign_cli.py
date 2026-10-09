from __future__ import annotations

import itertools
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from cluster_profiles import fleet_qualification_campaign_cli as campaign_cli
from cluster_profiles.fleet_qualification import QualificationError
from tests.cluster_profiles.consumer_outcomes import not_adopted

ALPHA = "vonk-forge/alpha"
BETA = "vonk-forge/beta"
GAMMA = "vonk-forge/gamma"
NODE_A = "spk_" + "1" * 32
NODE_B = "spk_" + "2" * 32
DIGESTS = {ALPHA: "a" * 64, BETA: "b" * 64, GAMMA: "c" * 64}
COVERAGE_IDS = {
    "single-host-restart": "4" * 64,
    "dual-rank-loss-recovery": "5" * 64,
    "dual-host-restart": "6" * 64,
}


def _coverage(mode: str, representative: str, members: list[str]) -> dict[str, Any]:
    return {
        "coverage_id": COVERAGE_IDS[mode],
        "failure_mode": mode,
        "representative_recipe": representative,
        "members": [
            {
                "recipe": key,
                "recipe_content_sha256": DIGESTS[key],
                "package_sha256": "d" * 64,
                "model_content_sha256s": ["e" * 64],
                "runtime_stack_sha256": "f" * 64,
                "topology_sha256": "0" * 64,
            }
            for key in members
        ],
        "shared": len(members) > 1,
        "equivalence_rationale": "Same runtime stack and topology.",
    }


def _row(sequence: int, key: str, node_count: int, refs: list[dict[str, Any]]):
    return {
        "sequence": sequence,
        "key": key,
        "content_sha256": DIGESTS[key],
        "node_count": node_count,
        "interface": "openai-service",
        "recipe_version": "1.0.0",
        "package": {
            "path": f"packages/{key.split('/')[1]}.tar.gz",
            "sha256": "d" * 64,
            "expected_bytes": 1,
            "media_type": "application/gzip",
        },
        "disposition": "actionable",
        "model_license_refs": [
            {
                "key": "vonk-forge/model",
                "content_sha256": "e" * 64,
                "spdx": "Apache-2.0",
                "url": "https://example.com/license",
                "attribution": [],
            }
        ],
        "qualification_inputs": [],
        "smoke_cases": ["A323"],
        "review_gates": [],
        "runtime_stack_sha256": "f" * 64,
        "topology_sha256": "0" * 64,
        "recovery_coverage_refs": refs,
    }


def _ref(coverage: Mapping[str, Any], role: str) -> dict[str, Any]:
    return {
        "coverage_id": coverage["coverage_id"],
        "failure_mode": coverage["failure_mode"],
        "representative_recipe": coverage["representative_recipe"],
        "role": role,
    }


def _batch(sequence: int, mode: str, recipe: str, node_count: int) -> dict[str, Any]:
    return {
        "sequence": sequence,
        "id": f"batch-{sequence:03d}",
        "mode": mode,
        "assignments": [{"recipe": recipe, "lane": 1, "node_count": node_count}],
    }


@pytest.fixture
def manifest(tmp_path: Path) -> Path:
    shared = _coverage("single-host-restart", ALPHA, [ALPHA, BETA])
    rank_loss = _coverage("dual-rank-loss-recovery", GAMMA, [GAMMA])
    restart = _coverage("dual-host-restart", GAMMA, [GAMMA])
    authority = {
        "schema_version": 5,
        "authority_id": "test-authority",
        "catalog": {
            "repository": "CarstVaartjes/vonk-forge-recipes",
            "commit": "1" * 40,
            "release_tag": "v2.0.0",
            "source_commit": "1" * 40,
            "catalog_index_sha256": "2" * 64,
            "qualification_index_sha256": "3" * 64,
            "recipe_count": 3,
        },
        "scope": {
            "maximum_node_count": 2,
            "recipe_count": 3,
            "excluded_topology_recipe_keys": [],
        },
        "recipes": [
            _row(1, ALPHA, 1, [_ref(shared, "representative")]),
            _row(2, BETA, 1, [_ref(shared, "shared-member")]),
            _row(
                3, GAMMA, 2, [_ref(rank_loss, "dedicated"), _ref(restart, "dedicated")]
            ),
        ],
        "batches": [
            _batch(1, "single", ALPHA, 1),
            _batch(2, "single", BETA, 1),
            _batch(3, "exclusive-dual", GAMMA, 2),
        ],
        "recovery_coverage": [shared, rank_loss, restart],
    }
    template = {
        "method": "POST",
        "path": "/chat/completions",
        "body": {"model": "$ALIAS", "messages": [{"role": "user", "content": "hi"}]},
        "assertions": [{"kind": "path.equals", "path": "model", "value": "$ALIAS"}],
        "max_response_bytes": 4096,
        "timeout_seconds": 60,
    }
    index = {
        "schema_version": 2,
        "fixtures": {},
        "recipes": {},
        "special_fixtures": {},
        "service_case_templates": {"A323": template},
        "service_recipes": {
            key: {
                "alias": key.split("/")[1],
                "content_sha256": digest,
                "smoke_cases": ["A323"],
            }
            for key, digest in DIGESTS.items()
        },
    }
    (tmp_path / "authority.json").write_text(json.dumps(authority))
    (tmp_path / "index.json").write_text(json.dumps(index))
    path = tmp_path / "campaign.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "qualification_authority": "authority.json",
                "fixture_manifest": "index.json",
                "options": {
                    "operation_timeout_seconds": 60,
                    "poll_interval_seconds": 1,
                },
            }
        )
    )
    return path


class FakeController:
    """A Controller whose profile loads converge at once."""

    def __init__(self) -> None:
        self.profile: dict[str, Any] = {"status": "not-created", "revision": 0}
        self.boot = {NODE_A: "boot-a", NODE_B: "boot-b"}
        self.loaded: dict[str, list[dict[str, Any]]] = {NODE_A: [], NODE_B: []}
        self.fail_next_load = False
        self.applications: dict[str, str] = {}
        self.accepted = {}

    def restart(self, node_ids: list[str]) -> None:
        for node_id in node_ids:
            self.boot[node_id] += "-restarted"

    def request(self, method: str, path: str, payload: Any = None, **_: Any) -> Any:
        if path == "/api/profile/1" and method == "GET":
            return dict(self.profile)
        if path == "/api/profile/1" and method == "PUT":
            if payload["expected_revision"] != self.profile["revision"]:
                from cluster_profiles.control_client import ControlConflict

                raise ControlConflict(409, "current owner revision is different")
            self.profile = {
                "status": "created",
                "revision": self.profile["revision"] + 1,
                "labels": payload["labels"],
                "assignments": payload["assignments"],
            }
            return dict(self.profile)
        if path.startswith("/api/profile/1/requests/"):
            key = path.rsplit("/", 1)[1]
            return self.accepted[key]
        if path == "/api/profile/1/load":
            key = payload["request_key"]
            if key in self.accepted:
                return self.accepted[key]
            application_id = f"app-{len(self.applications) + 1}"
            state = "failed" if self.fail_next_load else "succeeded"
            self.fail_next_load = False
            self.applications[application_id] = state
            if state == "succeeded":
                self._converge()
            self.accepted[key] = {"id": application_id, "request_key": key}
            return self.accepted[key]
        if path.startswith("/api/profile/applications/"):
            application_id = path.rsplit("/", 1)[1]
            return {
                "id": application_id,
                "state": self.applications[application_id],
                "status_reason": "x",
            }
        if path.startswith("/api/recipe/"):
            key = path.removeprefix("/api/recipe/").replace("%2F", "/")
            return {"identity": {"content_sha256": DIGESTS[key]}}
        if path == "/api/fleet":
            return {
                "nodes": [
                    {
                        "id": node_id,
                        "connection": {"online_state": "online"},
                        "telemetry": {"sample": {"boot_id": self.boot[node_id]}},
                        "loaded": self.loaded[node_id],
                    }
                    for node_id in (NODE_A, NODE_B)
                ]
            }
        raise AssertionError((method, path))

    def _converge(self) -> None:
        self.loaded = {NODE_A: [], NODE_B: []}
        for assignment in self.profile["assignments"]:
            for node_id in assignment["spark_ids"]:
                self.loaded[node_id].append(
                    {
                        "alias": assignment["assignment_name"],
                        "run_id": f"run-{assignment['assignment_name']}",
                        "healthy": True,
                        "route_state": "published",
                        "run_state": "running",
                    }
                )


@pytest.fixture
def failing_smoke(monkeypatch: pytest.MonkeyPatch) -> set[str]:
    """Replace the fixture smoke; recipes added to the set fail it."""

    failing: set[str] = set()

    def fake(client: Any, lane: Any, run_id: str, *_: Any) -> dict[str, object]:
        if lane.row.key in failing:
            raise QualificationError("smoke assertion failed")
        return {"run_id": run_id, "cases": [{"case_id": "smoke", "state": "succeeded"}]}

    monkeypatch.setattr(campaign_cli, "_smoke", fake)
    return failing


def _run(
    manifest: Path,
    controller: FakeController,
    *args: str,
    notices: list[Any] | None = None,
) -> dict[str, Any]:
    def notify(notice: dict[str, object]) -> None:
        if notices is not None:
            notices.append(notice)
        controller.restart(list(notice["sparks"]))  # type: ignore[arg-type]

    return campaign_cli.run(
        [
            *args,
            "--manifest",
            str(manifest),
            "--results",
            str(manifest.parent / "results.jsonl"),
            "--profile-number",
            "1",
        ],
        client_factory=lambda: controller,
        clock=itertools.count().__next__,
        sleeper=lambda _seconds: None,
        notify=notify,
    )


def test_status_plans_batches_from_the_authority(manifest: Path) -> None:
    result = _run(manifest, FakeController())

    assert result["status"] == "in-progress"
    assert result["recipe_count"] == 3
    assert result["next"] == {
        "batch": "batch-001",
        "mode": "single",
        "recipes": [ALPHA],
        "step": "load",
    }


def test_campaign_qualifies_every_batch_and_resumes_after_a_failed_load(
    manifest: Path, failing_smoke: set[str]
) -> None:
    controller = FakeController()
    controller.fail_next_load = True
    with not_adopted():
        _run(manifest, controller, "load", "--spark", NODE_A)
    # Nothing blocks a retry: the same step simply runs again.
    loaded = _run(manifest, controller, "load", "--spark", NODE_A)
    assert [item["status"] for item in loaded["results"]] == ["passed"]
    assert _run(manifest, controller)["next"]["step"] == "recover"

    notices: list[Any] = []
    recovered = _run(manifest, controller, "recover", notices=notices)
    assert [item["status"] for item in recovered["results"]] == ["passed"]
    assert notices[0]["sparks"] == [NODE_A]
    _run(manifest, controller, "stop")
    assert controller.profile["assignments"] == []

    # The shared member reuses its representative's passed recovery.
    _run(manifest, controller, "load", "--spark", NODE_B)
    notices.clear()
    covered = _run(manifest, controller, "recover", notices=notices)
    assert notices == []
    assert covered["results"][0]["status"] == "covered"
    assert covered["results"][0]["covered_by"] == ALPHA
    _run(manifest, controller, "stop")

    _run(manifest, controller, "load", "--spark", NODE_A, "--spark", NODE_B)
    _run(manifest, controller, "recover", notices=notices)
    assert [(item["checkpoint"], item["sparks"]) for item in notices] == [
        ("dual-rank-loss-recovery", [NODE_B]),
        ("dual-host-restart", [NODE_A, NODE_B]),
    ]
    _run(manifest, controller, "stop")

    final = _run(manifest, controller)
    assert final["status"] == "complete"
    assert final["qualified_recipe_count"] == 3


def test_failed_smoke_is_recorded_and_the_batch_can_still_be_stopped(
    manifest: Path, failing_smoke: set[str]
) -> None:
    failing_smoke.add(ALPHA)
    controller = FakeController()

    loaded = _run(manifest, controller, "load", "--spark", NODE_A)

    assert loaded["results"][0]["status"] == "failed"
    status = _run(manifest, controller)
    assert status["recipes"][ALPHA]["qualified"] is False
    assert status["next"]["step"] == "stop"
    _run(manifest, controller, "stop")
    assert _run(manifest, controller)["next"]["batch"] == "batch-002"


def test_a_profile_owned_by_something_else_is_never_overwritten(
    manifest: Path, failing_smoke: set[str]
) -> None:
    controller = FakeController()
    controller.profile = {"status": "created", "revision": 3, "labels": {}}

    with not_adopted():
        _run(manifest, controller, "load", "--spark", NODE_A)
    assert controller.profile["revision"] == 3
    controller.profile = {"status": "not-created", "revision": 0}
    loaded = _run(manifest, controller, "load", "--spark", NODE_A)
    assert loaded["results"][0]["run_id"] == "run-alpha"


def test_campaign_reconciles_lost_reply_and_restart_reuses_one_application(
    manifest, failing_smoke, monkeypatch
):
    from cluster_profiles.control_client import ControlTransportError

    class LostReply(FakeController):
        lost = False
        malformed = False

        def request(self, method, path, payload=None, **kwargs):
            value = super().request(method, path, payload, **kwargs)
            if path == "/api/profile/1/load" and not self.lost:
                self.lost = True
                raise ControlTransportError("reply lost after acceptance")
            if path.startswith("/api/profile/applications/") and not self.malformed:
                self.malformed = True
                return {}
            return value

    controller = LostReply()
    smoke = campaign_cli._smoke_lanes
    crashed = False

    def crash_after_acceptance(*args, **kwargs):
        nonlocal crashed
        if not crashed:
            crashed = True
            raise OSError("process ended before campaign evidence")
        return smoke(*args, **kwargs)

    monkeypatch.setattr(campaign_cli, "_smoke_lanes", crash_after_acceptance)
    with not_adopted():
        _run(manifest, controller, "load", "--spark", NODE_A)
    restarted = _run(manifest, controller, "load", "--spark", NODE_A)
    assert restarted["results"][0]["run_id"] == "run-alpha"
    assert len(controller.applications) == 1
    _run(manifest, controller, "load", "--spark", NODE_A)
    assert len(controller.applications) == 2
    _run(manifest, controller, "stop")
    assert controller.profile["assignments"] == []
    fresh = _run(
        manifest, controller, "load", "--batch", "batch-002", "--spark", NODE_A
    )
    assert fresh["results"][0]["run_id"] == "run-beta"
