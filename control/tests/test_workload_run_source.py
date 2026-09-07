from pathlib import Path

import pytest
from vonk_control.workload_run_source import (
    WorkloadRunParseError,
    parse_workload_run_yaml,
)

FIXTURES = Path(__file__).parent / "fixtures/workload_run"


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def test_minimal_recipe_parses_without_executing_command() -> None:
    source = parse_workload_run_yaml(fixture_bytes("minimal-vllm.yaml"))

    assert source.model == "Qwen/Qwen3-1.7B"
    assert source.runtime == "vllm"
    assert source.command.raw.startswith("vllm serve")
    assert source.source_sha256 == "6dea1f86c2081f1f21894588353735f5bc15a859928b0eeee5ab4c88ccdf99cb"
    assert "/command" in source.leaf_paths()


def test_unknown_fields_remain_visible_and_every_leaf_is_addressable() -> None:
    source = parse_workload_run_yaml(fixture_bytes("full-sglang.yaml"))

    assert [(item.path, item.value_type) for item in source.unknown_fields] == [
        ("/future_field", "mapping")
    ]
    assert "/future_field/enabled" in source.leaf_paths()
    assert len(source.leaf_paths()) == len(set(source.leaf_paths()))


@pytest.mark.parametrize(
    "body",
    [
        b"!!python/object:os.system ['id']",
        b"a: &a [*a]",
        b"a: 1\na: 2\n",
        b"a: 1\n---\nb: 2\n",
        b"a: !unknown value\n",
    ],
)
def test_unsafe_yaml_is_rejected(body: bytes) -> None:
    with pytest.raises(WorkloadRunParseError):
        parse_workload_run_yaml(body)


def test_size_depth_and_secret_environment_are_rejected() -> None:
    bodies = [
        b"x: " + b"a" * (256 * 1024),
        ("a: " * 34 + "value").encode(),
        fixture_bytes("malicious.yaml"),
    ]
    for body in bodies:
        with pytest.raises(WorkloadRunParseError):
            parse_workload_run_yaml(body)
