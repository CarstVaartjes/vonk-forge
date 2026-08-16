from pathlib import Path

from vonk_control.import_report import ImportDisposition
from vonk_control.recipe_contract import validate_recipe
from vonk_control.workload_run_importer import import_workload_run
from vonk_control.workload_run_source import parse_workload_run_yaml

FIXTURES = Path(__file__).parent / "fixtures/workload_run"


def test_every_source_leaf_has_exactly_one_report_item() -> None:
    source = parse_workload_run_yaml((FIXTURES / "full-sglang.yaml").read_bytes())
    result = import_workload_run(source)
    source_paths = set(source.leaf_paths())
    reported_source_paths = [
        item.source_path
        for item in result.report
        if not item.source_path.startswith("/@missing/")
    ]

    assert sorted(reported_source_paths) == sorted(source_paths)
    assert len(reported_source_paths) == len(set(reported_source_paths))
    assert {item.disposition for item in result.report} <= set(ImportDisposition)
    assert any(
        item.source_path.startswith("/mods/")
        and item.disposition is ImportDisposition.INCORPORATED
        for item in result.report
    )
    assert result.runnable is False


def test_container_and_mods_become_a_source_bundle() -> None:
    source = parse_workload_run_yaml((FIXTURES / "full-sglang.yaml").read_bytes())

    result = import_workload_run(source)

    dockerfile = result.bundle.files["Dockerfile"].decode()
    assert dockerfile.startswith("FROM ghcr.io/example/sglang@sha256:")
    assert 'LABEL ai.vonkforge.runtime-interface="v1"' in dockerfile
    assert "COPY mods/ /opt/vonk/mods/" in dockerfile
    assert result.draft_document["build"]["network"] == {
        "mode": "none",
        "hosts": [],
    }
    assert result.draft_document["build"]["context"]["sha256"] == result.bundle.sha256
    assert result.draft_document["topology"]["name"] == "nodes_2"
    validate_recipe(result.draft_document)


def test_import_is_deterministic_and_explains_missing_requirements() -> None:
    source = parse_workload_run_yaml((FIXTURES / "minimal-vllm.yaml").read_bytes())
    first = import_workload_run(source)
    second = import_workload_run(source)

    assert first == second
    assert first.source_sha256 == source.source_sha256
    assert first.report_digest == second.report_digest
    assert first.draft_document["provenance"]["source_kind"] == "workload_run"
    assert any(
        item.source_path == "/@missing/resources"
        and item.disposition is ImportDisposition.OVERLAY_REQUIRED
        for item in first.report
    )
    assert any(
        item.source_path == "/container"
        and item.disposition is ImportDisposition.RESOLUTION_REQUIRED
        for item in first.report
    )


def test_redacted_source_never_contains_secret_values() -> None:
    source = parse_workload_run_yaml(
        b"model: Example/Model\nruntime: vllm\ncommand: vllm serve Example/Model\ncredentials:\n  password: never-store-me\n"
    )

    result = import_workload_run(source)

    assert result.redacted_source["credentials"]["password"] == "<redacted>"
    assert "never-store-me" not in str(result.redacted_source)


def test_runtime_name_requires_exact_execution_harness_resolution() -> None:
    source = parse_workload_run_yaml(
        b"model: bartowski/Qwen-GGUF\n"
        b"model_revision: 0123456789abcdef0123456789abcdef01234567\n"
        b"runtime: llama.cpp\n"
        b"min_nodes: 1\nmax_nodes: 1\n"
        b"command: llama-server --model /models/qwen.gguf --port 8000\n"
    )

    result = import_workload_run(source)

    runtime_item = next(
        item for item in result.report if item.source_path == "/runtime"
    )
    assert runtime_item.disposition is ImportDisposition.RESOLUTION_REQUIRED
    assert runtime_item.destination_path == "/execution/harness"
    assert runtime_item.reason_code == "execution.harness"
    assert runtime_item.blocking is True
    assert result.draft_document["execution"]["harness"]["slug"] == "unresolved-harness"
    assert result.draft_document["runtime"]["distribution"]["slug"] == (
        "unresolved-distribution"
    )
    validate_recipe(result.draft_document)


def test_raw_workload_run_command_never_becomes_builtin_harness_authority() -> None:
    source = parse_workload_run_yaml((FIXTURES / "minimal-vllm.yaml").read_bytes())

    result = import_workload_run(source)

    command_item = next(
        item for item in result.report if item.source_path == "/command"
    )
    assert command_item.disposition is ImportDisposition.UNSUPPORTED_BLOCKING
    assert command_item.reason_code == "runtime.command_unsupported"
    assert result.draft_document["runtime"]["entrypoint"] == ["unresolved-runtime"]
    assert result.draft_document["runtime"]["arguments"] == []
    assert result.draft_document["runtime"]["environment"] == []
