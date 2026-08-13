import json
from pathlib import Path

import pytest
from vonk_control.recipe_contract import (
    RecipeContractError,
    canonical_recipe,
    deployment_profile,
    parse_recipe_json,
    recipe_content_sha256,
    validate_recipe,
)

ROOT = Path(__file__).resolve().parents[2]


def fixture(name: str) -> dict[str, object]:
    return json.loads((ROOT / "control/tests/fixtures/global" / name).read_text())


def contract_lock() -> dict[str, object]:
    return json.loads((ROOT / "schemas/global/contract.lock.json").read_text())


def test_recipe_hash_matches_global_fixture() -> None:
    expected = contract_lock()["fixtures"]["recipe-v1-minimal.json"]["content_sha256"]

    assert recipe_content_sha256(fixture("recipe-v1-minimal.json")) == expected


def test_vendored_recipe_contract_is_source_first() -> None:
    document = fixture("recipe-v1-minimal.json")

    assert document["build"]["context"]["sha256"] == "a" * 64
    assert "image" not in document["runtime"]


def test_vendored_multinode_profile_supports_three_nodes() -> None:
    profile = deployment_profile(fixture("recipe-v1-multinode.json"), "triple-tp3")

    assert profile["node_count"] == 3
    assert sum(role["count"] for role in profile["roles"]) == 3


def test_host_network_is_reserved_for_connected_multinode_profiles() -> None:
    multinode = fixture("recipe-v1-multinode.json")
    multinode["runtime"]["security"]["host_network"] = True

    validate_recipe(multinode)

    single = fixture("recipe-v1-minimal.json")
    single["runtime"]["security"]["host_network"] = True
    with pytest.raises(RecipeContractError, match="connected multi-node"):
        validate_recipe(single)


def test_canonical_recipe_matches_global_bytes() -> None:
    assert canonical_recipe({"z": 1, "a": [True, None]}) == (b'{"a":[true,null],"z":1}')


def test_recipe_parser_rejects_duplicate_keys_and_floats() -> None:
    with pytest.raises(RecipeContractError, match="duplicate object key"):
        parse_recipe_json(b'{"identity":{},"identity":{}}')
    with pytest.raises(RecipeContractError, match="floats are not permitted"):
        parse_recipe_json(b'{"value":1.5}')


@pytest.mark.parametrize(
    ("path", "value", "message"),
    [
        (("runtime", "image"), "ghcr.io/vonk/vllm:latest", "image"),
        (("runtime", "command"), ["sh", "-c", "id"], "command"),
        (("build", "dockerfile"), "../Dockerfile", "dockerfile"),
    ],
)
def test_recipe_validation_rejects_unsafe_values(path, value, message) -> None:
    document = fixture("recipe-v1-minimal.json")
    section, field = path
    document[section][field] = value

    with pytest.raises(RecipeContractError, match=message):
        validate_recipe(document)


def test_global_contract_lock_matches_vendored_bytes() -> None:
    lock = contract_lock()
    assert lock["source_commit"] == "5d09fd032b30de86154bd17ada3678ae55a7a0aa"
    for relative_path, metadata in lock["files"].items():
        payload = (ROOT / relative_path).read_bytes()
        assert __import__("hashlib").sha256(payload).hexdigest() == metadata["sha256"]


def test_vendored_runtime_policy_matches_agent_rootless_contract() -> None:
    policy = json.loads(
        (ROOT / "schemas/global/container-runtime-policy-v1.json").read_text()
    )

    assert policy["required_image_label"] == {
        "name": "ai.vonkforge.runtime-interface",
        "value": "v1",
    }
    assert policy["config_user_policy"] == {
        "kind": "numeric-non-root",
        "pattern": "^[1-9][0-9]*(?::[1-9][0-9]*)?$",
    }
    assert policy["host_isolation"] == "spark-docker-nvidia-compiled-helper"
