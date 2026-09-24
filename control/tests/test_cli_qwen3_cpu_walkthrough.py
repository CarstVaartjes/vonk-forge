"""Opt-in installed-CLI and real Qwen CPU execution for W19 U2."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from vonk_control.model_cache import ModelCacheService
from vonk_control.models import CatalogDocumentRevision
from vonk_control.recipe_runtime_specs import (
    compile_runtime_spec,
    resolve_recipe_entities,
)
from vonk_forge_contracts import ModelDefinition, RecipeDefinition, content_sha256
from vonk_forge_contracts.recipe import RecipeHttpServingRequest

from .test_cli_operator_walkthrough import (
    _QWEN3_BLOCKED_RECIPE_SELECTOR,
    _QWEN3_IMAGE_DIGEST,
    _QWEN3_RUNTIME_REPOSITORY,
    _QWEN3_RUNTIME_TAG,
    _READY_RECIPE_SELECTOR,
    _run_cli,
    _session_environment,
    _walkthrough_app,
    load_qwen_cpu_assets,
)
from .test_profile_load_installed_cli import _https_api_peer

pytest_plugins = ("tests.test_profile_load_installed_cli",)
pytestmark = [
    pytest.mark.lane,
    pytest.mark.skipif(
        os.environ.get("VONK_QWEN3_CPU_SMOKE") != "1",
        reason="set VONK_QWEN3_CPU_SMOKE=1 to use the verified local Qwen CPU assets",
    ),
]

_DEFAULT_ASSET_ROOT = Path("/private/tmp/vonk-cli-u2-qwen-assets")
_IMAGE_REFERENCE = f"{_QWEN3_RUNTIME_REPOSITORY}@{_QWEN3_IMAGE_DIGEST}"


def _docker(*arguments: str, timeout: float = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *arguments],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _assert_docker_ok(
    result: subprocess.CompletedProcess[str], description: str
) -> str:
    if result.returncode != 0:
        pytest.fail(
            f"{description} failed: "
            + (result.stderr.strip() or result.stdout.strip())[-2000:],
            pytrace=False,
        )
    return result.stdout


def _runtime_projection(
    sessions, revision_id: str
) -> tuple[RecipeDefinition, ModelDefinition, dict[str, object]]:
    with sessions() as session:
        revision = session.scalar(
            select(CatalogDocumentRevision).where(
                CatalogDocumentRevision.id == revision_id,
                CatalogDocumentRevision.kind == "recipe",
                CatalogDocumentRevision.state == "active",
            )
        )
        if revision is None or not isinstance(revision.document, dict):
            raise AssertionError("the ready recipe revision disappeared")
        recipe = RecipeDefinition.model_validate_json(
            json.dumps(
                revision.document,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            )
        )
        resolved = resolve_recipe_entities(session, revision.document)
        models = resolved.get("models")
        if not isinstance(models, tuple) or len(models) != 1:
            raise AssertionError("the Qwen recipe did not resolve one exact model")
        model_revision = models[0]
        if not isinstance(model_revision.document, dict):
            raise TypeError("the exact Qwen model revision is malformed")
        model = ModelDefinition.model_validate_json(
            json.dumps(
                model_revision.document,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            )
        )
        role = recipe.topology.roles[0]
        projection = compile_runtime_spec(
            recipe,
            resolved_entities=resolved,
            role=role.name,
            rank=0,
        )
    return recipe, model, projection


def _prepare_model_mount(
    *,
    cache: ModelCacheService,
    manifest_digest: str,
    model: ModelDefinition,
    artifacts: object,
    destination: Path,
) -> None:
    if not isinstance(artifacts, list):
        raise TypeError("compiled recipe artifacts are malformed")
    model_files = {item.path: item for item in model.files}
    observed_paths: set[str] = set()
    destination.mkdir(mode=0o755, parents=True, exist_ok=False)
    for item in artifacts:
        if not isinstance(item, dict):
            raise TypeError("compiled recipe artifact is malformed")
        relative = item.get("path")
        mount = item.get("mount")
        if (
            not isinstance(relative, str)
            or not isinstance(mount, dict)
            or mount.get("target") != "/models"
            or mount.get("read_only") is not True
        ):
            raise AssertionError("compiled model mount does not match its contract")
        model_file = model_files.get(relative)
        if model_file is None or relative in observed_paths:
            raise AssertionError("compiled model path is absent or repeated")
        observed_paths.add(relative)
        source, expected_bytes, expected_digest = cache.verified_artifact_file(
            manifest_digest, model_file.sha256, relative
        )
        if (
            expected_bytes != model_file.size_bytes
            or expected_digest != model_file.sha256
        ):
            raise AssertionError(
                "managed cache receipt differs from the model contract"
            )
        target = destination.joinpath(*PurePosixPath(relative).parts)
        target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        target.chmod(0o444)
        if target.stat().st_size != expected_bytes:
            raise AssertionError("materialized model file has an unexpected size")
        with target.open("rb") as stream:
            observed_digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if observed_digest != expected_digest:
            raise AssertionError("materialized model file differs from verified cache")
    if observed_paths != set(model_files):
        raise AssertionError("compiled runtime omitted part of the exact model set")


def _cache_mount_directories(runtime: dict[str, object], output_root: Path) -> Path:
    cache_root = output_root.parent / "managed-cache"
    cache_root.mkdir(mode=0o777)
    cache_root.chmod(0o777)
    writable_paths = runtime.get("writable_paths")
    if not isinstance(writable_paths, list):
        raise TypeError("compiled vLLM writable paths are unavailable")
    for item in writable_paths:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise TypeError("compiled vLLM writable path is malformed")
        path = PurePosixPath(item["path"])
        if path == PurePosixPath("/outputs/cache"):
            continue
        if path.is_absolute() is not True or path.parts[1] != "outputs":
            raise AssertionError("compiled writable path escaped /outputs")
        if len(path.parts) < 3:
            raise AssertionError("compiled writable path has no managed subdirectory")
        if path.parts[2] == "cache":
            relative = PurePosixPath(*path.parts[3:])
            target = cache_root.joinpath(*relative.parts)
        else:
            relative = PurePosixPath(*path.parts[2:])
            target = output_root.joinpath(*relative.parts)
        target.mkdir(mode=0o777, parents=True, exist_ok=True)
        target.chmod(0o777)
    return cache_root


def _start_runtime(
    *,
    container_name: str,
    projection: dict[str, object],
    image_id: str,
    model_root: Path,
    output_root: Path,
    cache_root: Path,
) -> str:
    runtime = projection.get("runtime")
    security = projection.get("security")
    if not isinstance(runtime, dict) or not isinstance(security, dict):
        raise TypeError("compiled execution projection is malformed")
    if (
        runtime.get("image") != _IMAGE_REFERENCE
        or runtime.get("architecture") != "linux/arm64"
        or security.get("read_only_root") is not True
        or security.get("network_mode") != "none"
        or security.get("no_new_privileges") is not True
        or security.get("capabilities") != []
        or security.get("user") != "10001:10001"
    ):
        raise AssertionError(
            "compiled Qwen runtime differs from canonical launch policy"
        )
    command = runtime.get("entrypoint")
    environment = runtime.get("environment")
    mounts = security.get("mounts")
    if (
        not isinstance(command, list)
        or not command
        or any(not isinstance(value, str) for value in command)
        or not isinstance(environment, list)
        or not isinstance(mounts, list)
    ):
        raise AssertionError(
            "compiled Qwen command, environment, or mounts are malformed"
        )
    mount_targets = {
        item.get("target"): item
        for item in mounts
        if isinstance(item, dict) and isinstance(item.get("target"), str)
    }
    if set(mount_targets) != {"/models", "/outputs"}:
        raise AssertionError("compiled Qwen mounts are incomplete or unexpected")
    if (
        mount_targets["/models"].get("read_only") is not True
        or mount_targets["/outputs"].get("read_only") is not False
    ):
        raise AssertionError("compiled model/output mount permissions are invalid")

    arguments = [
        "run",
        "--detach",
        "--name",
        container_name,
        "--platform",
        "linux/arm64",
        "--memory",
        "8g",
        "--cpus",
        "4",
        "--shm-size",
        "1g",
        "--read-only",
        "--network",
        "none",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        "4096",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,mode=1777,size=1073741824",
        "--mount",
        f"type=bind,source={model_root},target=/models,readonly",
        "--mount",
        f"type=bind,source={output_root},target=/outputs",
        "--mount",
        f"type=bind,source={cache_root},target=/outputs/cache",
    ]
    for item in environment:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("name"), str)
            or not isinstance(item.get("value"), str)
        ):
            raise TypeError("compiled environment entry is malformed")
        arguments.extend(("--env", f"{item['name']}={item['value']}"))
    # These CPU smoke controls are test-only. They do not alter any of the
    # platform-owned writable paths emitted by the runtime compiler.
    arguments.extend(
        (
            "--env",
            "HF_HUB_OFFLINE=1",
            "--env",
            "TRANSFORMERS_OFFLINE=1",
            "--env",
            "VLLM_CPU_KVCACHE_SPACE=1",
            "--env",
            "OMP_NUM_THREADS=4",
            "--env",
            "MKL_NUM_THREADS=4",
            "--user",
            str(security["user"]),
            "--entrypoint",
            command[0],
            image_id,
            *command[1:],
        )
    )
    result = _docker(*arguments, timeout=30)
    return _assert_docker_ok(result, "starting the pinned CPU runtime").strip()


def _request_in_container(
    container: str,
    *,
    method: str,
    path: str,
    body: Mapping[str, object] | None = None,
    timeout: int = 10,
) -> dict[str, object]:
    body_bytes = b"" if body is None else json.dumps(dict(body)).encode("utf-8")
    encoded = base64.b64encode(body_bytes).decode("ascii")
    code = (
        "import base64,json,urllib.request;"
        f"payload=base64.b64decode('{encoded}');"
        f"request=urllib.request.Request('http://127.0.0.1:8000{path}',"
        f"data=payload if {body is not None!r} else None,"
        f"headers={{'Content-Type':'application/json'}} if {body is not None!r} else {{}},"
        f"method='{method}');"
        f"print(urllib.request.urlopen(request,timeout={timeout}).read().decode())"
    )
    result = _docker("exec", container, "python", "-c", code, timeout=timeout + 10)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    value = json.loads(result.stdout)
    if not isinstance(value, dict):
        raise TypeError("local inference endpoint returned a non-object JSON value")
    return value


def _wait_for_model(container: str, alias: str, timeout_seconds: int = 180) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_error = "runtime did not become ready"
    while time.monotonic() < deadline:
        state = _docker(
            "inspect", "--format", "{{.State.Running}}", container, timeout=10
        )
        if state.returncode != 0 or state.stdout.strip() != "true":
            logs = _docker("logs", "--tail", "80", container, timeout=10)
            detail = logs.stdout.strip() or logs.stderr.strip() or last_error
            pytest.fail(f"pinned CPU runtime exited before readiness: {detail[-4000:]}")
        try:
            response = _request_in_container(
                container, method="GET", path="/v1/models", timeout=3
            )
            models = response.get("data")
            if isinstance(models, list) and any(
                isinstance(item, dict) and item.get("id") == alias for item in models
            ):
                return
            last_error = "runtime returned no exact model alias"
        except (OSError, RuntimeError, json.JSONDecodeError) as error:
            last_error = str(error)
        time.sleep(2)
    logs = _docker("logs", "--tail", "80", container, timeout=10)
    detail = logs.stdout.strip() or logs.stderr.strip() or last_error
    pytest.fail(f"pinned CPU runtime did not become ready: {detail[-4000:]}")


def test_installed_cli_later_page_qwen_candidate_runs_real_cpu_inference(
    postgres_engine,
    installed_vonkctl: Path,
) -> None:
    asset_root = Path(
        os.environ.get("VONK_QWEN3_CPU_ASSET_ROOT", str(_DEFAULT_ASSET_ROOT))
    )
    if not Path("/private/tmp").is_dir():
        pytest.skip("this opt-in fixture uses the shared OrbStack /private/tmp mount")
    assets = load_qwen_cpu_assets(asset_root)

    database_workspace: Path
    with tempfile.TemporaryDirectory(
        prefix="vonk-cli-u2-qwen-", dir="/private/tmp"
    ) as temporary:
        workspace = Path(temporary)
        database_workspace = workspace
        workspace.chmod(0o700)
        (
            sessions,
            app,
            headers,
            _node_id,
            _candidate_revision_id,
            _candidate_digest,
            ready_revision_id,
            ready_digest,
        ) = _walkthrough_app(
            postgres_engine,
            workspace / "owners",
            qwen_cpu_assets=assets,
        )
        operator_cwd = workspace / "operator"
        operator_cwd.mkdir(mode=0o700)
        with (
            TestClient(app) as api,
            _https_api_peer(workspace, api, headers) as (url, certificate, _peer),
        ):
            environment = _session_environment(
                installed_vonkctl=installed_vonkctl,
                workspace=workspace,
                url=url,
                certificate=certificate,
                headers=headers,
            )
            page = _run_cli(
                installed_vonkctl,
                (
                    "--no-input",
                    "--json",
                    "recipe",
                    "library",
                    "--all-models",
                    "--fits-fleet",
                    "--limit",
                    "1",
                    "--sort",
                    "name",
                ),
                environment,
                operator_cwd,
            )
            assert page.returncode == 0, page.stdout + page.stderr
            page_document = json.loads(page.stdout)
            recipes = page_document.get("recipes")
            cursor = page_document.get("next_cursor")
            assert isinstance(recipes, list) and len(recipes) == 1
            first_candidate = recipes[0]
            assert (
                first_candidate["selector"]
                == f"vonk-forge/{_QWEN3_BLOCKED_RECIPE_SELECTOR}"
            )
            first_assessment = first_candidate.get("assessment")
            assert isinstance(first_assessment, dict)
            assert first_assessment["fleet_fit"]["state"] == "ready"
            assert first_assessment["cache"]["state"] == "blocked"
            assert isinstance(cursor, str) and cursor
            ready_page = _run_cli(
                installed_vonkctl,
                (
                    "--no-input",
                    "--json",
                    "recipe",
                    "library",
                    "--all-models",
                    "--fits-fleet",
                    "--limit",
                    "1",
                    "--sort",
                    "name",
                    "--cursor",
                    cursor,
                ),
                environment,
                operator_cwd,
            )
            assert ready_page.returncode == 0, ready_page.stdout + ready_page.stderr
            ready_document = json.loads(ready_page.stdout)
            ready_rows = ready_document.get("recipes")
            assert isinstance(ready_rows, list) and len(ready_rows) == 1
            candidate = ready_rows[0]
            assert candidate["selector"] == f"vonk-forge/{_READY_RECIPE_SELECTOR}"
            assert candidate["identity"]["recipe_revision_id"] == ready_revision_id
            assert candidate["identity"]["content_sha256"] == ready_digest
            assessment = candidate.get("assessment")
            assert isinstance(assessment, dict)
            assert assessment["fleet_fit"]["state"] == "ready"
            assert assessment["cache"]["state"] == "ready"
            assert assessment["readiness"]["state"] == "ready"
            assert candidate["selector"] != (
                f"vonk-forge/{_QWEN3_BLOCKED_RECIPE_SELECTOR}"
            )

            recipe, model, projection = _runtime_projection(sessions, ready_revision_id)
            if model.identity != assets.model.identity:
                raise AssertionError("ready recipe selected a different Qwen identity")
            runtime = projection.get("runtime")
            endpoint = projection.get("endpoint")
            if not isinstance(runtime, dict) or not isinstance(endpoint, dict):
                raise TypeError("compiled Qwen runtime endpoint is unavailable")
            if runtime.get("image") != _IMAGE_REFERENCE:
                raise AssertionError("compiled image is not the pinned OCI manifest")

            cache = ModelCacheService(
                sessions,
                workspace / "owners" / "model-cache",
                reserve_bytes=0,
            )
            manifest = cache.resolve_artifact_set(
                model_content_sha256=content_sha256(model)
            )
            model_root = workspace / "run" / "models"
            _prepare_model_mount(
                cache=cache,
                manifest_digest=manifest.digest,
                model=model,
                artifacts=projection.get("artifacts"),
                destination=model_root,
            )

            run_root = workspace / "run"
            output_root = run_root / "outputs"
            output_root.mkdir(mode=0o777, parents=True)
            output_root.chmod(0o777)
            cache_root = _cache_mount_directories(runtime, output_root)

            context = _assert_docker_ok(
                _docker("context", "show"), "reading the Docker context"
            ).strip()
            assert context == "orbstack", f"expected OrbStack, found {context}"
            engine = _assert_docker_ok(
                _docker("info", "--format", "{{.OSType}} {{.Architecture}}"),
                "reading the OrbStack engine platform",
            ).strip()
            engine_os, _, engine_architecture = engine.partition(" ")
            assert engine_os == "linux"
            assert engine_architecture in {"aarch64", "arm64"}, engine
            image_loaded = _docker("image", "inspect", _QWEN3_RUNTIME_TAG)
            if image_loaded.returncode != 0:
                _assert_docker_ok(
                    _docker(
                        "load",
                        "--input",
                        str(assets.runtime_archive),
                        timeout=240,
                    ),
                    "loading the verified CPU image archive",
                )
                image_loaded = _docker("image", "inspect", _QWEN3_RUNTIME_TAG)
            image_document = json.loads(
                _assert_docker_ok(image_loaded, "inspecting the verified CPU image")
            )[0]
            assert image_document["Os"] == "linux"
            assert image_document["Architecture"] == "arm64"
            labels = image_document["Config"]["Labels"]
            assert labels["ai.vonkforge.runtime-interface"] == "v1"
            skopeo = shutil.which("skopeo")
            assert skopeo is not None, (
                "Skopeo is required to verify the local image tag"
            )
            daemon_manifest = subprocess.run(
                [skopeo, "inspect", "--raw", f"docker-daemon:{_QWEN3_RUNTIME_TAG}"],
                capture_output=True,
                timeout=30,
                check=False,
            )
            if daemon_manifest.returncode != 0:
                pytest.fail(
                    "inspecting the local pinned image manifest failed: "
                    + daemon_manifest.stderr.decode("utf-8", errors="replace")[-2000:],
                    pytrace=False,
                )
            observed_manifest = hashlib.sha256(daemon_manifest.stdout).hexdigest()
            assert (
                f"sha256:{observed_manifest}" == assets.image_evidence.manifest_digest
            )
            image_id = image_document.get("Id")
            assert isinstance(image_id, str) and image_id

            container_name = f"vonk-u2-qwen-{uuid4().hex[:12]}"
            try:
                _start_runtime(
                    container_name=container_name,
                    projection=projection,
                    image_id=image_id,
                    model_root=model_root,
                    output_root=output_root,
                    cache_root=cache_root,
                )
                aliases = endpoint.get("model_aliases")
                if (
                    not isinstance(aliases, list)
                    or not aliases
                    or not isinstance(aliases[0], str)
                    or not aliases[0]
                ):
                    raise AssertionError("compiled Qwen model alias is missing")
                _wait_for_model(container_name, aliases[0])

                checks = recipe.validation.serving.checks
                check = next(item for item in checks if item.kind == "openai.chat")
                request = check.request
                if not isinstance(request, RecipeHttpServingRequest):
                    raise TypeError("Qwen serving check is not an HTTP request")
                body = dict(request.body or {})
                body["model"] = aliases[0]
                response = _request_in_container(
                    container_name,
                    method=request.method,
                    path=request.path,
                    body=body,
                    timeout=150,
                )
                choices = response.get("choices")
                assert isinstance(choices, list) and choices
                choice = choices[0]
                assert isinstance(choice, dict)
                message = choice.get("message")
                assert isinstance(message, dict)
                content = message.get("content")
                assert isinstance(content, str) and content.strip()
                assert choice.get("finish_reason") == "stop", response
                usage = response.get("usage")
                assert isinstance(usage, dict)
                max_tokens = body.get("max_tokens")
                completion_tokens = usage.get("completion_tokens")
                assert type(max_tokens) is int and max_tokens > 0
                assert type(completion_tokens) is int
                assert completion_tokens <= max_tokens
            finally:
                _docker("rm", "--force", container_name, timeout=30)

    assert not database_workspace.exists()
