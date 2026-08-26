from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient
from vonk_control.api import create_app
from vonk_control.audit import MemoryAuditStore
from vonk_control.auth import Actor, TokenCodec
from vonk_control.cluster_mappings import ClusterMappingPlacement, ClusterMappingPlan
from vonk_control.install_admission import InstallNodePlan, InstallPlan
from vonk_control.recipe_action_plans import (
    ActionReason,
    StopNodeImpact,
    StopPlan,
    UninstallConsequences,
    UninstallNodeImpact,
    UninstallPlan,
)
from vonk_control.recipe_builds import RecipeBuildPlan
from vonk_control.recipe_operations import (
    ImageDistributionPreview,
    RecipeOperationView,
    RecipeRunRankStatus,
    RecipeRunStatus,
)
from vonk_control.run_admission import RunNodePlan, RunPlan
from vonk_control.source_policy import SourcePolicyReport

NODE = "spk_" + "1" * 32
REVISION = "00000000-0000-4000-8000-000000000001"
INSTALLATION = "00000000-0000-4000-8000-000000000002"
RUN = "00000000-0000-4000-8000-000000000003"
OPERATION = "00000000-0000-4000-8000-000000000004"
MAPPING = "00000000-0000-4000-8000-000000000005"
BUILD = "00000000-0000-4000-8000-000000000006"
NOW = datetime(2026, 8, 7, 12, tzinfo=UTC)


class Jobs:
    def list(self, **_kwargs):
        return []

    def get(self, _job_id):
        raise KeyError


class Recipes:
    def __init__(self) -> None:
        self.install_plan = InstallPlan(
            mapping_id=MAPPING,
            mapping_generation=1,
            recipe_build_id=BUILD,
            image_digest="sha256:" + "d" * 64,
            recipe_revision_id=REVISION,
            recipe_content_sha256="a" * 64,
            allowed=True,
            nodes=(
                InstallNodePlan(
                    node_id=NODE,
                    rank=0,
                    role="entrypoint",
                    allowed=True,
                    inventory_observed_at=NOW,
                    free_bytes=1000,
                    active_reserved_bytes=0,
                    reused_bytes=0,
                    required_download_bytes=100,
                    required_bytes=120,
                    disk_floor_bytes=10,
                    free_after_bytes=880,
                    blockers=(),
                    warnings=(),
                ),
            ),
            plan_digest="b" * 64,
        )
        self.run_plan = RunPlan(
            installation_id=INSTALLATION,
            alias="qwen",
            mapping_id=MAPPING,
            mapping_generation=1,
            recipe_revision_id=REVISION,
            allowed=True,
            nodes=(
                RunNodePlan(
                    node_id=NODE,
                    rank=0,
                    role="entrypoint",
                    endpoint_owner=True,
                    port=8000,
                    allowed=True,
                    inventory_observed_at=NOW,
                    memory_kind="unified",
                    required_memory_bytes=200,
                    available_memory_bytes=500,
                    active_reserved_bytes=0,
                    free_after_bytes=300,
                    memory_floor_bytes=50,
                    fabric_address=None,
                    fabric_bandwidth_mbps=None,
                    rendezvous_port=None,
                    blockers=(),
                    warnings=(),
                ),
            ),
            plan_digest="c" * 64,
        )
        self.stop_plan = StopPlan(
            run_id=RUN,
            installation_id=INSTALLATION,
            recipe_revision_id=REVISION,
            alias="qwen",
            run_state="running",
            route_state="published",
            route_generation=7,
            route_digest="d" * 64,
            authority_digest="c" * 64,
            allowed=True,
            route_withdrawal=True,
            nodes=(StopNodeImpact(NODE, 0, "entrypoint", "running", 200, 200),),
            total_active_memory_reservation_bytes=200,
            blockers=(),
            warnings=(
                ActionReason(
                    "stop.capacity_release_deferred",
                    "Capacity remains reserved until every selected rank stops.",
                ),
            ),
            plan_digest="4" * 64,
        )
        self.uninstall_plan = UninstallPlan(
            installation_id=INSTALLATION,
            recipe_id="00000000-0000-4000-8000-000000000007",
            recipe_revision_id=REVISION,
            recipe_content_sha256="a" * 64,
            recipe_content={"schema_version": 1},
            installation_authority_digest="a" * 64,
            original_plan_digest="b" * 64,
            installation_state="installed",
            allowed=True,
            nodes=(UninstallNodeImpact(NODE, 0, "entrypoint", "installed", 120),),
            bytes_removed=120,
            active_runs=(),
            active_run_count=0,
            active_runs_truncated=False,
            blockers=(),
            warnings=(),
            consequences=UninstallConsequences(),
            plan_digest="5" * 64,
        )
        self.calls: list[tuple[str, object]] = []
        self.started_run: tuple[RecipeOperationView, str, str, str] | None = None
        self.mapping_plan = ClusterMappingPlan(
            recipe_revision_id=REVISION,
            recipe_content_sha256="a" * 64,
            topology_name="solo",
            generation=1,
            parameters={},
            nodes=(ClusterMappingPlacement(NODE, 0, "entrypoint", True),),
            placement_digest="e" * 64,
        )
        self.build_plan = RecipeBuildPlan(
            build_id=BUILD,
            recipe_revision_id=REVISION,
            recipe_content_sha256="a" * 64,
            builder_node_id=NODE,
            source_bundle_sha256="f" * 64,
            build_input_sha256="1" * 64,
            agent_payload={"operation": "recipe.build.v1"},
        )

    def preview_mapping(self, revision, nodes, *, parameters, actor):
        self.calls.append(("preview_mapping", (revision, nodes, parameters, actor)))
        return self.mapping_plan

    def create_mapping(self, plan, **kwargs):
        self.calls.append(("create_mapping", (plan, kwargs)))
        return MAPPING

    def check_build_source(self, revision):
        self.calls.append(("check_source", revision))
        return SourcePolicyReport(True, "f" * 64, "Dockerfile", ())

    def preview_build(self, revision, builder):
        self.calls.append(("preview_build", (revision, builder)))
        return self.build_plan

    def build(self, plan, **kwargs):
        self.calls.append(("build", (plan, kwargs)))
        return RecipeOperationView(
            OPERATION,
            "recipe.build",
            BUILD,
            "running",
            plan.build_input_sha256,
            (NODE,),
            None,
        )

    def distribute_image(self, build_id, mapping_id, **kwargs):
        self.calls.append(("distribute_image", (build_id, mapping_id, kwargs)))
        return RecipeOperationView(
            OPERATION,
            "recipe.image.distribute",
            BUILD,
            "running",
            "2" * 64,
            (NODE,),
            None,
        )

    def preview_image_distribution(self, build_id, mapping_id, **kwargs):
        self.calls.append(
            ("preview_image_distribution", (build_id, mapping_id, kwargs))
        )
        return ImageDistributionPreview(
            recipe_build_id=BUILD,
            mapping_id=MAPPING,
            mapping_generation=1,
            image_digest="sha256:" + "d" * 64,
            node_ids=(NODE,),
            plan_digest="2" * 64,
        )

    def preview_install(self, mapping, build):
        self.calls.append(("preview_install", (mapping, build)))
        return self.install_plan

    def install(self, plan, **kwargs):
        self.calls.append(("install", kwargs))
        return RecipeOperationView(
            OPERATION,
            "recipe.install",
            INSTALLATION,
            "running",
            plan.plan_digest,
            (NODE,),
            None,
        )

    def preview_run(self, installation, alias):
        self.calls.append(("preview_run", (installation, alias)))
        return self.run_plan

    def replay_start(self, installation, alias, plan_digest, request_id):
        self.calls.append(
            (
                "replay_start",
                (installation, alias, plan_digest, request_id),
            )
        )
        if self.started_run is None:
            return None
        operation, expected_installation, expected_alias, expected_request_id = (
            self.started_run
        )
        if (
            installation == expected_installation
            and alias == expected_alias
            and plan_digest == operation.plan_digest
            and request_id == expected_request_id
        ):
            return operation
        return None

    def start(self, plan, **kwargs):
        self.calls.append(("start", kwargs))
        operation = RecipeOperationView(
            OPERATION, "recipe.start", RUN, "running", plan.plan_digest, (NODE,), None
        )
        self.started_run = (
            operation,
            plan.installation_id,
            plan.alias,
            kwargs["request_id"],
        )
        return operation

    def stop(self, run_id, **kwargs):
        self.calls.append(("stop", (run_id, kwargs)))
        return RecipeOperationView(
            OPERATION, "recipe.stop", run_id, "running", "4" * 64, (NODE,), None
        )

    def preview_stop(self, run_id):
        self.calls.append(("preview_stop", run_id))
        return self.stop_plan

    def run_status(self, run_id):
        if run_id != RUN:
            raise KeyError(run_id)
        return RecipeRunStatus(
            id=RUN,
            alias="qwen",
            state="running",
            route_state="withdrawn",
            healthy=False,
            ranks=(
                RecipeRunRankStatus(
                    node_id=NODE,
                    rank=0,
                    role="entrypoint",
                    state="failed",
                    observed_at=NOW,
                    age_seconds=0.0,
                    fresh=True,
                ),
            ),
        )

    def uninstall(self, installation_id, **kwargs):
        self.calls.append(("uninstall", (installation_id, kwargs)))
        return RecipeOperationView(
            OPERATION,
            "recipe.uninstall",
            installation_id,
            "running",
            "5" * 64,
            (NODE,),
            None,
        )

    def preview_uninstall(self, installation_id):
        self.calls.append(("preview_uninstall", installation_id))
        return self.uninstall_plan

    def retry(self, operation_id, **kwargs):
        self.calls.append(("retry", (operation_id, kwargs)))
        return RecipeOperationView(
            OPERATION,
            "recipe.install",
            INSTALLATION,
            "running",
            "b" * 64,
            (NODE,),
            None,
        )

    def get(self, operation_id):
        self.calls.append(("get", operation_id))
        return RecipeOperationView(
            operation_id,
            "recipe.install",
            INSTALLATION,
            "succeeded",
            "b" * 64,
            (NODE,),
            {"successful_nodes": [NODE]},
        )


def setup():
    codec = TokenCodec(b"r" * 32)
    audits = MemoryAuditStore()
    recipes = Recipes()
    app = create_app(
        jobs=Jobs(),
        tokens=codec,
        audits=audits,
        fleet=lambda: {"nodes": []},
        now=lambda: 10,
        recipe_operations=recipes,
    )

    def headers(role="administrator"):
        token = codec.issue(Actor(role, role), ttl_seconds=100, now=0)
        return {"Authorization": f"Bearer {token}"}

    return TestClient(app), headers, recipes, audits


def test_preview_install_and_run_expose_exact_capacity_math() -> None:
    client, headers, _recipes, _audits = setup()
    install = client.post(
        "/api/v1/recipes/install-plans/preview",
        headers=headers(),
        json={"mapping_id": MAPPING, "recipe_build_id": BUILD},
    )
    run = client.post(
        "/api/v1/recipes/run-plans/preview",
        headers=headers(),
        json={"installation_id": INSTALLATION, "alias": "qwen"},
    )

    assert install.status_code == run.status_code == 200
    assert install.json()["nodes"][0]["free_after_bytes"] == 880
    assert run.json()["nodes"][0]["required_memory_bytes"] == 200
    assert run.json()["alias"] == "qwen"
    assert _recipes.calls[-1] == ("preview_run", (INSTALLATION, "qwen"))
    assert len(install.json()["plan_digest"]) == 64


def test_source_gate_build_and_cluster_mapping_are_explicit_steps() -> None:
    client, headers, recipes, audits = setup()
    checked = client.post(
        "/api/v1/recipes/source-checks",
        headers=headers(),
        json={"recipe_revision_id": REVISION},
    )
    mapping_preview = client.post(
        "/api/v1/recipes/mapping-plans/preview",
        headers=headers(),
        json={
            "recipe_revision_id": REVISION,
            "node_ids": [NODE],
            "parameters": {},
        },
    )
    mapping = client.post(
        "/api/v1/recipes/mappings",
        headers=headers(),
        json={
            "recipe_revision_id": REVISION,
            "node_ids": [NODE],
            "parameters": {},
            "placement_digest": "e" * 64,
            "request_key": "10000000-0000-4000-8000-000000000010",
        },
    )
    build_preview = client.post(
        "/api/v1/recipes/build-plans/preview",
        headers=headers(),
        json={"recipe_revision_id": REVISION, "builder_node_id": NODE},
    )
    built = client.post(
        "/api/v1/recipes/builds",
        headers=headers(),
        json={
            "recipe_revision_id": REVISION,
            "builder_node_id": NODE,
            "build_input_sha256": "1" * 64,
            "request_key": "10000000-0000-4000-8000-000000000011",
        },
    )
    distribution_preview = client.post(
        "/api/v1/recipes/image-distribution-plans/preview",
        headers=headers(),
        json={
            "recipe_build_id": BUILD,
            "mapping_id": MAPPING,
            "mapping_generation": 1,
        },
    )
    distributed = client.post(
        "/api/v1/recipes/image-distributions",
        headers=headers(),
        json={
            "recipe_build_id": BUILD,
            "mapping_id": MAPPING,
            "mapping_generation": 1,
            "plan_digest": "2" * 64,
            "request_key": "10000000-0000-4000-8000-000000000012",
        },
    )

    assert checked.json()["passed"] is True
    assert mapping_preview.json()["topology_name"] == "solo"
    assert mapping_preview.json()["nodes"][0]["rank"] == 0
    assert mapping.status_code == 201
    assert build_preview.json()["build_input_sha256"] == "1" * 64
    assert distribution_preview.json()["node_ids"] == [NODE]
    assert built.status_code == distributed.status_code == 202
    assert {event.action for event in audits.list()} >= {
        "recipe.mapping.create",
        "recipe.build",
        "recipe.image.distribute",
    }
    assert [name for name, _ in recipes.calls] == [
        "check_source",
        "preview_mapping",
        "preview_mapping",
        "create_mapping",
        "preview_build",
        "preview_build",
        "build",
        "preview_image_distribution",
        "distribute_image",
    ]


def test_execute_requires_exact_plan_hash_admin_and_idempotency_key() -> None:
    client, headers, recipes, audits = setup()
    denied = client.post(
        "/api/v1/recipes/installations",
        headers=headers("operator"),
        json={
            "mapping_id": MAPPING,
            "recipe_build_id": BUILD,
            "plan_digest": "b" * 64,
            "request_key": "10000000-0000-4000-8000-000000000001",
        },
    )
    request_id = "20000000-0000-4000-8000-000000000001"
    accepted = client.post(
        "/api/v1/recipes/installations",
        headers={**headers(), "x-request-id": request_id},
        json={
            "mapping_id": MAPPING,
            "recipe_build_id": BUILD,
            "plan_digest": "b" * 64,
            "request_key": "10000000-0000-4000-8000-000000000001",
        },
    )

    assert denied.status_code == 403
    assert accepted.status_code == 202
    assert accepted.json()["owner_id"] == INSTALLATION
    assert recipes.calls[-1][0] == "install"
    assert audits.for_request(request_id).action == "recipe.install"


def test_start_progress_stop_retry_and_uninstall_routes_are_stable() -> None:
    client, headers, _recipes, _audits = setup()
    start = client.post(
        "/api/v1/recipes/runs",
        headers=headers(),
        json={
            "installation_id": INSTALLATION,
            "alias": "qwen",
            "plan_digest": "c" * 64,
            "request_key": "10000000-0000-4000-8000-000000000002",
        },
    )
    progress = client.get(
        f"/api/v1/recipes/operations/{OPERATION}", headers=headers("viewer")
    )
    stop_preview = client.post(
        "/api/v1/recipes/stop-plans/preview",
        headers=headers(),
        json={"run_id": RUN},
    )
    stop = client.post(
        f"/api/v1/recipes/runs/{RUN}/stop",
        headers=headers(),
        json={
            "plan_digest": "4" * 64,
            "request_key": "10000000-0000-4000-8000-000000000003",
        },
    )
    retry = client.post(
        f"/api/v1/recipes/operations/{OPERATION}/retry",
        headers=headers(),
        json={"request_key": "10000000-0000-4000-8000-000000000004"},
    )
    uninstall_preview = client.post(
        "/api/v1/recipes/uninstall-plans/preview",
        headers=headers(),
        json={"installation_id": INSTALLATION},
    )
    uninstall = client.post(
        f"/api/v1/recipes/installations/{INSTALLATION}/uninstall",
        headers=headers(),
        json={
            "plan_digest": "5" * 64,
            "request_key": "10000000-0000-4000-8000-000000000005",
        },
    )

    assert {
        start.status_code,
        stop.status_code,
        retry.status_code,
        uninstall.status_code,
    } == {202}
    assert progress.status_code == 200
    assert stop_preview.status_code == uninstall_preview.status_code == 200
    assert stop_preview.json()["total_active_memory_reservation_bytes"] == 200
    assert uninstall_preview.json()["consequences"] == {
        "catalog_retained": True,
        "automatic_stop": False,
        "reinstall_required": True,
    }
    paths = client.get("/openapi.json").json()["paths"]
    assert (
        paths["/api/v1/recipes/install-plans/preview"]["post"]["operationId"]
        == "previewRecipeInstall"
    )
    assert paths["/api/v1/recipes/runs"]["post"]["operationId"] == "startRecipeRun"
    assert (
        paths["/api/v1/recipes/stop-plans/preview"]["post"]["operationId"]
        == "previewRecipeStop"
    )
    assert (
        paths["/api/v1/recipes/uninstall-plans/preview"]["post"]["operationId"]
        == "previewRecipeUninstall"
    )


def test_identical_start_api_replay_audits_without_repreview_or_mutation() -> None:
    client, headers, recipes, audits = setup()
    body = {
        "installation_id": INSTALLATION,
        "alias": "qwen",
        "plan_digest": "c" * 64,
        "request_key": "10000000-0000-4000-8000-000000000009",
    }
    first_request_id = "20000000-0000-4000-8000-000000000009"
    replay_request_id = "20000000-0000-4000-8000-000000000010"

    first = client.post(
        "/api/v1/recipes/runs",
        headers={**headers(), "x-request-id": first_request_id},
        json=body,
    )
    replayed = client.post(
        "/api/v1/recipes/runs",
        headers={**headers(), "x-request-id": replay_request_id},
        json=body,
    )

    assert first.status_code == replayed.status_code == 202
    assert replayed.json() == first.json()
    assert [name for name, _details in recipes.calls] == [
        "replay_start",
        "preview_run",
        "start",
        "replay_start",
    ]
    assert [
        (
            audits.for_request(request_id).actor,
            audits.for_request(request_id).action,
            audits.for_request(request_id).targets,
        )
        for request_id in (first_request_id, replay_request_id)
    ] == [
        ("administrator", "recipe.start", (RUN, "c" * 64, NODE)),
        ("administrator", "recipe.start", (RUN, "c" * 64, NODE)),
    ]


def test_stop_and_uninstall_contracts_are_admin_only_strict_and_digest_bound() -> None:
    client, headers, _recipes, _audits = setup()

    denied = client.post(
        "/api/v1/recipes/stop-plans/preview",
        headers=headers("operator"),
        json={"run_id": RUN},
    )
    extra = client.post(
        "/api/v1/recipes/uninstall-plans/preview",
        headers=headers(),
        json={"installation_id": INSTALLATION, "force": True},
    )
    stop_unbound = client.post(
        f"/api/v1/recipes/runs/{RUN}/stop",
        headers=headers(),
        json={"request_key": "10000000-0000-4000-8000-000000000006"},
    )
    uninstall_unbound = client.post(
        f"/api/v1/recipes/installations/{INSTALLATION}/uninstall",
        headers=headers(),
        json={"request_key": "10000000-0000-4000-8000-000000000007"},
    )

    assert denied.status_code == 403
    assert extra.status_code == 422
    assert stop_unbound.status_code == uninstall_unbound.status_code == 422


def test_administrator_run_status_is_typed_rank_health_without_secrets() -> None:
    client, headers, _recipes, _audits = setup()

    response = client.get(f"/api/v1/recipes/runs/{RUN}", headers=headers())
    denied = client.get(f"/api/v1/recipes/runs/{RUN}", headers=headers("operator"))

    assert response.status_code == 200
    assert denied.status_code == 403
    assert response.json() == {
        "id": RUN,
        "alias": "qwen",
        "state": "running",
        "route_state": "withdrawn",
        "healthy": False,
        "ranks": [
            {
                "node_id": NODE,
                "rank": 0,
                "role": "entrypoint",
                "state": "failed",
                "observed_at": "2026-08-07T12:00:00Z",
                "age_seconds": 0.0,
                "fresh": True,
            }
        ],
    }
    assert not {
        "endpoint",
        "evidence_digest",
        "management_address",
        "certificate",
        "token",
    } & set(response.text.split('"'))
    paths = client.get("/openapi.json").json()["paths"]
    assert (
        paths["/api/v1/recipes/runs/{run_id}"]["get"]["operationId"]
        == "getRecipeRunStatus"
    )
