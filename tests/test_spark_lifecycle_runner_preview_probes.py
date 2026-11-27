"""Spark lifecycle runner: preview probes."""

from __future__ import annotations

import ast
import functools
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from vonk_forge_contracts import RecipeDefinition

from control.tests.token_support import TokenCodec
from scripts.fleet.development_slice_client import Client
from tests.acceptance import artifact_job_canary as job
from tests.acceptance.controller_contract import ControllerContract
from tests.spark_lifecycle_runner_support import _module
from vonk_agent_protocol.compiled_execution_plan import CompiledExecutionPlan
from vonk_agent_protocol.recipe_operations import RecipeStartPayload
from vonk_control.catalog.service import CatalogService
from vonk_control.catalog.sync import ManagedRecipeCatalogSyncService
from vonk_control.compiled_artifact_contract import compile_artifact_contract
from vonk_control.fleet_profile_contract.assessment import (
    FleetProfileAdmissionDecision,
)
from vonk_control.harnesses.canonical import validate_runtime_policy
from vonk_control.model_primitives import Base
from vonk_control.recipe_operations.start_payloads import (
    RecipeStartPlacement,
    build_recipe_start_payload,
)
from vonk_control.source_bundles import SourceBundleStore


@pytest.mark.parametrize("foreign_host", [False, True])
def test_lost_start_probe_binds_produced_placement_to_owned_interface(
    tmp_path: Path,
    foreign_host: bool,
) -> None:

    lifecycle = _module()
    address = "172.31.42.1"
    identifier = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    node_id = "spk_" + "b" * 32
    fixture = json.loads(
        (
            Path(__file__).parent.parent
            / "control/tests/fixtures/compiled_workload_v2.json"
        ).read_text(encoding="utf-8")
    )
    payload = RecipeStartPayload.model_validate(
        build_recipe_start_payload(
            run_id=identifier,
            installation_id=identifier,
            recipe_revision_id=identifier,
            mapping_id=identifier,
            run_generation=1,
            plan_digest="c" * 64,
            placement=RecipeStartPlacement(
                node_id, 0, "entrypoint", 8000, 80_000_000, 0, "unified", None
            ),
            compiled_endpoint_address=address,
            world_size=1,
            compiled_execution_plan=CompiledExecutionPlan.model_validate(fixture),
            master_address=None,
            master_port=None,
            peer_addresses=(),
        )
    )
    placement = payload.compiled_execution_plan.runtime.placement
    endpoint = f"http://{'172.31.43.1' if foreign_host else placement.endpoint_address}:{placement.port}"
    run = lifecycle.SparkLifecycle.__new__(lifecycle.SparkLifecycle)
    run.bundle = tmp_path
    run.temporary_root = tmp_path
    run.arguments = SimpleNamespace(output=tmp_path / "report.json")
    run.synthetic_fabric_octet = 42
    run.synthetic_interfaces = ["vmgt42"]
    run._lost_start_placement = None
    run.synthetic_canary_fixture = SimpleNamespace(serving_check={}, slug="canary")
    commands = []

    def command(argv, **_kwargs):
        commands.append(argv)
        assert argv == ["/usr/sbin/ip", "-j", "-4", "address", "show", "dev", "vmgt42"]
        return subprocess.CompletedProcess(
            argv,
            0,
            json.dumps(
                [
                    {
                        "ifname": "vmgt42",
                        "addr_info": [{"family": "inet", "local": address}],
                    }
                ]
            ),
            "",
        )

    run._run_command = command
    run._run_canonical_inference = lambda *_args, **_kwargs: "verified-response"
    run._observe_start_topology(
        endpoint,
        str(placement.endpoint_address),
        str(placement.port),
        identifier,
        identifier,
        node_id,
        identifier,
    )
    if foreign_host:
        with pytest.raises(lifecycle.LifecycleError):
            run._direct_canary_inference(endpoint)
    else:
        assert run._direct_canary_inference(endpoint) == "verified-response"
    evidence = json.loads((tmp_path / "failed-start-topology.json").read_text())
    assert evidence["accepted_address"] == address
    assert evidence["interface_addresses"] == [address]
    assert evidence["receipt_address"] == ("172.31.43.1" if foreign_host else address)
    assert len(commands) == 1


def test_artifact_job_stub_writes_its_result_or_fails_with_stderr(
    tmp_path: Path,
) -> None:
    """The stub the job canary installs, run with the flags the job harness passes."""

    script = tmp_path / job.JOB_SCRIPT_NAME
    script.write_bytes(job.JOB_SCRIPT)
    outputs = tmp_path / "outputs"
    outputs.mkdir()

    def run(exit_code: int) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                script,
                "--exit-code",
                str(exit_code),
                "--output-dir",
                str(outputs),
            ],
            capture_output=True,
            text=True,
            check=False,
        )

    succeeded = run(0)
    assert succeeded.returncode == 0
    assert [path.name for path in outputs.iterdir()] == [job.RESULT_NAME]
    assert (outputs / job.RESULT_NAME).read_bytes() == job.RESULT_BYTES
    (outputs / job.RESULT_NAME).unlink()
    failed = run(job.FAILURE_EXIT_CODE)
    assert failed.returncode == job.FAILURE_EXIT_CODE
    assert job.FAILURE_MARKER in failed.stderr
    assert list(outputs.iterdir()) == []


def test_artifact_job_recipe_is_imported_by_the_controller_catalog(
    tmp_path: Path, monkeypatch
) -> None:
    """The derived job Recipe passes the Controller's own catalog sync and policy."""
    pytest.importorskip("vonk_control", reason="requires the control environment")
    library = os.environ.get("VONK_RECIPE_LIBRARY_ROOT")
    if not library:
        pytest.skip("set VONK_RECIPE_LIBRARY_ROOT to the canonical recipe library")
    monkeypatch.setenv(
        "VONK_SYNTHETIC_CANARY_INDEX",
        "tests/fixtures/canonical-synthetic-canary/index.json",
    )

    lifecycle = _module()
    original = lifecycle._canonical_canary_fixture(Path(library))
    fixture = lifecycle._artifact_job_fixture(original)
    original_source = json.loads(original.index_bytes)["recipes"][0][
        "source_bundle_sha256"
    ]
    editorial = lifecycle._editorial_successor(original)
    assert (
        json.loads(editorial.index_bytes)["recipes"][0]["source_bundle_sha256"]
        == original_source
    )
    recipe = RecipeDefinition.model_validate(fixture.recipe)
    validate_runtime_policy(recipe)
    contract = compile_artifact_contract(recipe, "artifact-job")
    assert [parameter.name for parameter in contract.parameters] == ["exit_code"]

    specification = importlib.util.spec_from_file_location(
        "spark_canary_catalog_import", lifecycle.CANARY_CATALOG_IMPORT
    )
    assert specification is not None
    assert specification.loader is not None
    program = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(program)
    index = json.loads(fixture.index_bytes)
    digest = index["recipes"][0]["package"]["sha256"]
    archive = program._cache_package(
        tmp_path / "packages", fixture.package_bytes, digest
    )
    engine = create_engine(f"sqlite:///{tmp_path / 'catalog.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)

    def clock():
        return datetime(2026, 10, 15, tzinfo=UTC)

    reader = program.FixtureReader(index, {digest: archive})
    # Copying the original context digest after adding the job used to make
    # candidate acceptance fail at package ingress. Editorial changes reuse it.
    [item] = reader.snapshot.items
    assert item.source_bundle_sha256 == index["recipes"][0]["source_bundle_sha256"]
    assert item.source_bundle_sha256 != original_source
    assert reader.fetch(item.uri).source_bundle_sha256 == item.source_bundle_sha256
    sync = ManagedRecipeCatalogSyncService(
        sessions,
        catalog=CatalogService(
            sessions,
            clock=clock,
            cursors=TokenCodec(b"s" * 32).cursor_codec(),
            source_bundles=SourceBundleStore(tmp_path / "bundles"),
        ),
        reader=reader,
        clock=clock,
    )

    view = program.sync_canary_catalog(
        sync,
        request_key="eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeef",
        snapshot=reader.snapshot,
    )

    assert (view.state, view.commit, view.imported_count, view.problems) == (
        "current",
        fixture.source_commit,
        1,
        (),
    )


def test_artifact_job_canary_speaks_the_controller_contract() -> None:
    """Every request the job canary sends, and every answer it reads, is the contract's.

    A scripted Controller answers with documents its own OpenAPI accepts; the
    canary must drive two jobs to success and one to failure with evidence.
    """

    # The operator CLI's copy of the Controller contract.
    openapi = (
        Path(__file__).parents[1] / "src/cluster_profiles/schemas/control-openapi.json"
    )
    if not openapi.is_file():
        pytest.skip("generate the CLI contract with generate-control-clients")
    contract = ControllerContract(json.loads(openapi.read_text()), label="source")
    run_id = str(uuid.uuid4())
    jobs: dict[str, dict[str, object]] = {}
    result = {
        "name": job.RESULT_NAME,
        "media_type": "text/plain",
        "size_bytes": len(job.RESULT_BYTES),
        "sha256": hashlib.sha256(job.RESULT_BYTES).hexdigest(),
    }
    stamp = "2026-10-15T20:00:00Z"
    tail = {"truncated": False, "dropped_bytes": 0, "dropped_lines": 0}
    request_ids: list[str] = []

    def answer(
        method: str, target: str, body: bytes | None
    ) -> tuple[int, dict[str, object] | bytes]:
        path, _, query = target.partition("?")
        if method == "POST" and path.endswith("/artifact-jobs"):
            create = json.loads(body or b"{}")
            job_id = str(uuid.uuid4())
            jobs[job_id] = {
                "id": job_id,
                "run_id": run_id,
                "interface": "artifact-job",
                "state": None,
                "preparation": "draft",
                "contract_sha256": "a" * 64,
                "compiled_contract": None,
                "input_manifest_sha256": "b" * 64,
                "input_total_bytes": 0,
                "input_declarations": None,
                "input_files": [],
                "output_limits": create["output_limits"],
                "output_files": [],
                "timeout_seconds": create["timeout_seconds"],
                "created_at": stamp,
                "updated_at": stamp,
                "exit_code": create["parameters"]["exit_code"],
            }
            return 201, jobs[job_id]
        identifier = path.split("/")[3]
        if path.endswith("/finalize"):
            jobs[identifier]["preparation"] = "ready"
        elif path.endswith("/submit"):
            failing = jobs[identifier]["exit_code"] != 0
            jobs[identifier].update(
                preparation=None,
                operation_id=identifier,
                state="failed" if failing else "succeeded",
                output_files=[] if failing else [result],
            )
            return 202, jobs[identifier]
        elif path.startswith("/api/operations/") and path.endswith("/evidence"):
            assert query == "attempt=1"
            return 200, {
                "schema_version": 2,
                "context": {
                    "operation_id": identifier,
                    "attempt": 1,
                    "kind": "recipe.job.run.v1",
                    "node_ids": [],
                    "updated_at": stamp,
                    "source": "agent",
                },
                "collected_at": stamp,
                "summary": "job adapter exited unsuccessfully",
                "error_code": "artifact_process_failed",
                "diagnostics": {
                    "schema_version": 1,
                    "collected_at": stamp,
                    "phase": "recipe.job.run.v1",
                    "category": "runtime",
                    "stdout": {"text": "", **tail},
                    "stderr": {"text": job.FAILURE_MARKER + "\n", **tail},
                    "versions": [],
                    "sandbox": [],
                    "storage": [],
                    "preflight": [],
                    "collector_errors": [],
                },
                "collector_errors": [],
            }
        elif "/results/" in path:
            return 200, job.RESULT_BYTES
        return 200, jobs[identifier]

    def transport(
        method: str,
        path: str,
        body: bytes | None,
        headers: dict[str, str],
        _timeout: float,
    ) -> tuple[int, bytes]:
        contract.check(method, path, body)
        if path.endswith(("/artifact-jobs", "/submit")):
            request_ids.append(headers["X-Request-ID"])
        status, document = answer(method, path, body)
        if isinstance(document, bytes):
            return status, document
        view = {key: value for key, value in document.items() if key != "exit_code"}
        contract.check_json_response(method, path, view, status=status)
        return status, json.dumps(view).encode()

    control = Client("https://controller", "token", timeout=5, transport=transport)
    job.run_artifact_jobs(
        control,
        run_id=run_id,
        request_key=lambda stage: str(uuid.uuid5(uuid.NAMESPACE_URL, stage)),
        container_names=set,
    )

    exit_codes = sorted(int(str(item["exit_code"])) for item in jobs.values())
    assert exit_codes == [0, 0, job.FAILURE_EXIT_CODE]
    assert len(set(request_ids)) == 6
    assert all(re.fullmatch(r"[0-9a-f-]{36}", value) for value in request_ids)


def test_preview_diagnostic_names_assignment_level_admission_blockers() -> None:
    pytest.importorskip(
        "fastapi", reason="Controller contract tests run in the control suite"
    )

    lifecycle = _module()

    def decision(assignment_id: str, *, blocked: bool) -> dict[str, object]:
        blockers = (
            [
                {
                    "code": "memory.insufficient",
                    "detail": "not enough memory",
                    "severity": "blocker",
                    "scope": "node",
                    "node_ids": ["spk_" + "b" * 32],
                }
            ]
            if blocked
            else []
        )
        return FleetProfileAdmissionDecision.model_validate(
            {
                "assignment_id": assignment_id,
                "alias": None,
                "allowed": not blocked,
                "blockers": blockers,
                "requirements": [],
                "effective_settings": None,
                "stops": [],
                "stop_before_prepare": False,
                "stop_before_transfer": False,
            }
        ).model_dump(mode="json")

    blocked_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    allowed = [
        decision(f"bbbbbbbb-bbbb-4bbb-8bbb-{index:012d}", blocked=False)
        for index in range(9)
    ]
    preview = {
        "allowed": False,
        "summary": {"blockers": 1},
        "reasons": [{"code": "profile.interruption_expected", "severity": "warning"}],
        "admission_decisions": [*allowed, decision(blocked_id, blocked=True)],
    }
    admission = json.loads(lifecycle.SparkLifecycle._preview_diagnostic(preview))[
        "admission"
    ]
    assert [item["assignment_id"] for item in admission] == [blocked_id]
    assert admission[0]["blockers"][0]["code"] == "memory.insufficient"
    assert admission[0]["blockers"][0]["detail"] == "not enough memory"
    assert admission[0]["blockers"][0]["node_ids"] == ["spk_" + "b" * 32]


NODE = "spk_" + "1" * 32


def _canary_lane_under_withdrawal(
    monkeypatch, previews: list[dict[str, object]], *, parked_loads: int = 0
):
    """A lane whose Controller withdraws the canary until a preview is admitted.

    ``previews`` are the profile previews the Controller answers in order. The
    lane stops at the profile load, the first step after the re-offered serve.
    """
    lifecycle = _module()

    sha = "a" * 64
    fixture = lifecycle.CanonicalCanaryFixture(
        index_path=Path("index.json"),
        index_bytes=b"{}",
        package_path=PurePosixPath("package.tar.gz"),
        package_bytes=b"",
        source_commit="a" * 40,
        publisher="vonk-forge-test",
        slug="canonical-synthetic-canary",
        recipe_content_sha256=sha,
        model_content_sha256="c" * 64,
        role="entrypoint",
        serving_check={},
        recipe={},
    )
    monkeypatch.setattr(
        lifecycle,
        "LibraryRecipeIdentity",
        SimpleNamespace(
            model_validate_json=lambda _value: SimpleNamespace(
                recipe_revision_id="revision",
                publisher=fixture.publisher,
                slug=fixture.slug,
                content_sha256=sha,
            )
        ),
    )
    monkeypatch.setattr(lifecycle, "_canonical_recipe_matches", lambda *_args: True)
    imported: list[str] = []
    downloads: list[str] = []
    remaining = list(previews)

    class Control:
        def request(self, method, path, body=None, allowed=None):
            if path == "/api/catalog/managed-recipes/sync-status":
                return 200, {
                    "trigger": "automatic",
                    "state": "current",
                    "commit": "c" * 40,
                    "withdrawn_count": 2,
                }
            if (method, path) == (
                "GET",
                f"/api/recipe/{fixture.publisher}/{fixture.slug}",
            ):
                return 200, {
                    "identity": {},
                    "selector": f"{fixture.publisher}/{fixture.slug}",
                    "document": {},
                    "model_selectors": ["model"],
                }
            if method == "PUT":
                return 200, {"revision": 1}
            assert (method, path) == ("POST", "/api/profile/1/preview")
            return 200, remaining.pop(0)

    lane = lifecycle.SparkLifecycle.__new__(lifecycle.SparkLifecycle)
    lane.control = Control()
    lane.browser = object()
    lane.synthetic_canary_fixture = fixture
    lane._import_canary_catalog = lambda _fixture, key: (
        imported.append(key)
        or {
            "state": "current",
            "commit": fixture.source_commit,
            "problems": [],
            "total_count": 1,
            "processed_count": 1,
            "imported_count": 1,
            "unchanged_count": 0,
        }
    )
    lane._fleet_snapshot = lambda: {"nodes": [{"id": NODE}]}
    lane._request_recipe_download = lambda _selector, *, request_key: (
        downloads.append(request_key) or {}
    )
    lane._await_recipe_download = lambda *_args, **_kwargs: {
        "result": {
            "recipe_content_sha256": sha,
            "model_content_digests": [fixture.model_content_sha256],
            "image_digest": "sha256:" + "d" * 64,
            "oci_archive_sha256": "e" * 64,
            "image_bytes": 1,
        }
    }

    lane.loads = loads = []
    lane.awaited = awaited = []

    def load(_preview, *, request_key):
        loads.append(request_key)
        if len(loads) <= parked_loads:
            raise lifecycle.LifecycleError("the profile load parked and timed out")
        return {}

    def await_load(*_args, **_kwargs):
        awaited.append(True)
        raise lifecycle.LifecycleError("reached the load await")

    lane._load_canary_profile = load
    lane._await_profile_application = await_load
    lane._installation_failure = lambda stage, error: lifecycle.LifecycleError(
        f"{stage}: {error}"
    )
    return lifecycle, lane, imported, downloads


_WITHDRAWN_PREVIEW = {
    "allowed": False,
    "reasons": [{"code": "profile.recipe_unavailable", "severity": "error"}],
    "preparations": [],
}


_ADMITTED_PREVIEW = {
    "allowed": True,
    "plan_digest": "f" * 64,
    "scope": {"node_ids": [NODE], "idle_node_ids": []},
    "preparations": [
        {
            "preparation": {
                "model": {
                    "model_content_sha256": "c" * 64,
                    "artifact_set_sha256": "1" * 64,
                    "artifact_set_bytes": 1,
                },
                "runtime_image": {
                    "image_digest": "sha256:" + "d" * 64,
                    "oci_layout_sha256": "2" * 64,
                    "image_bytes": 1,
                },
            }
        }
    ],
}


def test_a_canary_the_library_withdrew_is_offered_again_and_served(
    monkeypatch,
) -> None:
    lifecycle, lane, imported, downloads = _canary_lane_under_withdrawal(
        monkeypatch, [_WITHDRAWN_PREVIEW, _ADMITTED_PREVIEW]
    )

    with pytest.raises(lifecycle.LifecycleError, match="reached the load await"):
        lane._run_synthetic_canary(NODE, carry=lambda: None)

    # One re-import, and the second attempt downloads under its own request key.
    assert len(imported) == 2
    assert imported[0] != imported[1]
    assert len(downloads) == 2
    assert downloads[0] != downloads[1]
    assert len(lane.loads) == 1


def test_a_canary_withdrawn_between_preview_and_load_is_offered_again(
    monkeypatch,
) -> None:
    """The Controller previews again at load time: the retry covers the load too."""
    lifecycle, lane, imported, downloads = _canary_lane_under_withdrawal(
        monkeypatch, [_ADMITTED_PREVIEW, _ADMITTED_PREVIEW], parked_loads=1
    )

    with pytest.raises(lifecycle.LifecycleError, match="reached the load await"):
        lane._run_synthetic_canary(NODE, carry=lambda: None)

    assert len(imported) == 2
    assert len(downloads) == 2
    # The second load has its own request key, and only it was awaited.
    assert len(lane.loads) == 2
    assert lane.loads[0] != lane.loads[1]
    assert lane.awaited == [True]


def test_a_canary_withdrawn_twice_fails_with_the_catalog_sync_status(
    monkeypatch,
) -> None:
    lifecycle, lane, imported, _downloads = _canary_lane_under_withdrawal(
        monkeypatch, [_WITHDRAWN_PREVIEW, _WITHDRAWN_PREVIEW]
    )

    with pytest.raises(lifecycle.LifecycleError) as raised:
        lane._run_synthetic_canary(NODE)

    message = str(raised.value)
    assert "withdrew the fixture again" in message
    assert "profile.recipe_unavailable" in message
    assert '"trigger": "automatic"' in message
    assert '"withdrawn_count": 2' in message
    assert len(imported) == 2


def test_an_unreadable_sync_status_never_replaces_the_canary_failure(
    monkeypatch,
) -> None:
    lifecycle, lane, _imported, _downloads = _canary_lane_under_withdrawal(
        monkeypatch, [_WITHDRAWN_PREVIEW]
    )
    request = lane.control.request

    def without_sync_status(method, path, *args, **kwargs):
        if path == "/api/catalog/managed-recipes/sync-status":
            raise AssertionError((method, path))
        return request(method, path, *args, **kwargs)

    lane.control.request = without_sync_status

    with pytest.raises(lifecycle.LifecycleError) as raised:
        lane._run_synthetic_canary(NODE, carry=lambda: None)

    message = str(raised.value)
    assert "profile.recipe_unavailable" in message
    assert "catalog sync status unknown" in message


def _calls(node, name: str) -> bool:

    return any(
        isinstance(call, ast.Call) and getattr(call.func, "attr", None) == name
        for call in ast.walk(node)
    )


def _serves_without_admission(tree) -> list[str]:
    """Serves offered once more that do not reach the load and its await.

    The Controller previews again when it loads, so a serve that ends at the
    preview leaves a withdrawal between preview and load outside the retry.
    """

    functions = {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    missing: list[str] = []
    for call in ast.walk(tree):
        if not (
            isinstance(call, ast.Call)
            and getattr(call.func, "attr", None) == "_offered_once_more"
            and call.args
        ):
            continue
        names = {
            node.attr if isinstance(node, ast.Attribute) else node.id
            for node in ast.walk(call.args[0])
            if isinstance(node, ast.Attribute | ast.Name)
        }
        if not any(
            name in functions
            and _calls(functions[name], "_load_canary_profile")
            and _calls(functions[name], "_await_profile_application")
            for name in names
        ):
            missing.append(f"line {call.lineno}")
    return missing


def _unguarded_fixture_imports(
    node, enclosing: tuple[ast.FunctionDef | ast.AsyncFunctionDef, ...] = ()
) -> list[str]:

    found: list[str] = []
    for child in ast.iter_child_nodes(node):
        if not isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
            found += _unguarded_fixture_imports(child, enclosing)
            continue
        functions = (*enclosing, child)
        if (
            child.name not in {"_import_canary_catalog", "_import_candidate_library"}
            and _calls(child, "_import_canary_catalog")
            and not any(_calls(f, "_offered_once_more") for f in functions)
        ):
            found.append(child.name)
        found += _unguarded_fixture_imports(child, functions)
    return found


def test_every_fixture_import_in_the_lane_code_is_offered_once_more() -> None:
    """A fixture import that is then served must go through _offered_once_more.

    The live recipe library can be republished while a lane runs, which
    withdraws the fixture by design. Each function that imports a fixture must
    sit inside, or be, a function that calls ``_offered_once_more``, and the
    serve it wraps must reach the profile load and its await. The
    candidate library is only imported: the carry that follows serves the
    workload that already runs.
    """

    root = Path(__file__).parent / "acceptance"
    unguarded: list[str] = []
    for name in ("test_spark_lifecycle.py", "spark_upgrade_carry.py"):
        tree = ast.parse((root / name).read_text(encoding="utf-8"))
        unguarded += [f"{name}:{f}" for f in _unguarded_fixture_imports(tree)]
        unguarded += [
            f"{name}: no load in the serve at {w}"
            for w in _serves_without_admission(tree)
        ]
    assert unguarded == []


def _blocked(code: str) -> dict[str, Any]:
    return {
        "allowed": False,
        "admission_decisions": [{"allowed": False, "blockers": [{"code": code}]}],
    }


_SWAP = "run-switch.resource.insufficient_swap"


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def sleep(self, seconds: float) -> None:
        self.now += seconds


@functools.cache
def _lane():
    return _module()


def _await(
    responses: list[dict[str, Any]], clock: _Clock, deadline_seconds: float = 270.0
) -> dict[str, Any]:
    queue = iter(responses)
    return _lane().await_admitted_preview(
        lambda: next(queue),
        label="preview",
        describe=lambda preview: json.dumps(preview, sort_keys=True),
        clock=lambda: clock.now,
        sleep=clock.sleep,
        deadline_seconds=deadline_seconds,
    )


def test_preview_waits_on_typed_waits_then_returns_the_admitted_answer() -> None:
    clock = _Clock()
    admitted = {"allowed": True}

    assert _await([_blocked(_SWAP), _blocked(_SWAP), admitted], clock) is admitted
    assert clock.now == 6.0


def test_preview_refusal_fails_at_once_without_waiting() -> None:
    clock = _Clock()

    with pytest.raises(_lane().LifecycleError, match="not admitted"):
        _await([_blocked(_SWAP.replace("swap", "other")), {"allowed": True}], clock)
    assert clock.now == 0.0
    with pytest.raises(_lane().LifecycleError, match="not admitted"):
        _await([{"allowed": False}], clock)
    assert clock.now == 0.0


def test_preview_wait_past_the_deadline_fails_with_the_last_reason() -> None:
    clock = _Clock()

    with pytest.raises(
        _lane().LifecycleError, match="still waiting.*insufficient_swap"
    ):
        _await([_blocked(_SWAP)] * 100, clock, deadline_seconds=60.0)
    assert clock.now <= 60.0


def test_a_wait_beside_an_error_reason_is_a_refusal() -> None:
    clock = _Clock()
    preview = _blocked(_SWAP) | {"reasons": [{"severity": "error", "code": "x"}]}

    with pytest.raises(_lane().LifecycleError, match="not admitted"):
        _await([preview, {"allowed": True}], clock)
    assert clock.now == 0.0


def test_the_raw_profile_preview_request_lives_only_in_the_admitted_preview() -> None:
    """Catches a single-shot preview that fails on a wait the next cycle clears."""
    acceptance = Path(__file__).parent / "acceptance"
    found: list[tuple[str, str]] = []
    for path in sorted(acceptance.glob("*.py")):
        tree = ast.parse(path.read_text())
        for function in ast.walk(tree):
            if not isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            found.extend(
                (path.name, function.name)
                for node in ast.walk(function)
                if isinstance(node, ast.Constant)
                and node.value == "/api/profile/1/preview"
            )
        module_level = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and node.value == "/api/profile/1/preview"
        ]
        assert len(module_level) == len(
            [item for item in found if item[0] == path.name]
        ), f"{path.name}: preview path outside a function"
    assert set(found) == {("test_spark_lifecycle.py", "_admitted_preview")}
