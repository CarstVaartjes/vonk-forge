"""The real recipe catalog stays consumable by the Controller compiler.

The fixtures are verbatim recipe and model documents from a signed recipe
release, one representative per engine x topology x interface x option shape
(see ``catalog_launch_fixtures``). Compiling them must reproduce the launch
payloads committed under ``agent_protocol/tests/fixtures/catalog-launch``,
which the Rust agent's own tests deserialise, so neither the Controller
compiler nor the agent wire can drift from the recipe contract unnoticed.

Needs no recipe library checkout: everything is committed.
"""

from __future__ import annotations

import copy
import json
from typing import Any, get_args

import pytest
from vonk_agent_protocol import validate_compiled_execution_plan
from vonk_control.compiled_execution_plan import CompiledExecutionPlanError
from vonk_control.harnesses.canonical_metadata import CANONICAL_HARNESSES
from vonk_control.recipe_runtime_specs import RecipeRuntimeSpecError
from vonk_forge_contracts import document_sha256, read_model, read_recipe
from vonk_forge_contracts.recipe import RecipeJobInterface

from .catalog_launch_fixtures import (
    PAYLOADS,
    compile_recipe_payloads,
    expected_payload_names,
    load_inputs,
)

RECIPES, MODELS = load_inputs()
REFRESH = "refresh with: cd control && uv run python -m tests.catalog_launch_fixtures payloads"


def _committed(name: str) -> dict[str, Any]:
    path = PAYLOADS / name
    assert path.is_file(), f"missing launch payload fixture {name}; {REFRESH}"
    return json.loads(path.read_text(encoding="utf-8"))


def test_fixture_documents_are_valid_published_contract_documents() -> None:
    assert RECIPES and MODELS
    for digest, document in MODELS.items():
        assert document_sha256(document) == digest
        read_model(document)
    referenced: set[str] = set()
    for slug, document in RECIPES.items():
        recipe = read_recipe(document)
        assert recipe.identity.slug == slug
        referenced |= {item.model.content_sha256 for item in recipe.models}
    assert referenced == set(MODELS)


def test_fixtures_cover_every_engine_topology_and_interface_the_contract_allows() -> (
    None
):
    recipes = [read_recipe(document) for document in RECIPES.values()]
    assert {item.runtime.engine for item in recipes} == {
        harness.slug for harness in CANONICAL_HARNESSES
    }
    distributed = {item.runtime.engine for item in recipes if item.topology.distributed}
    capable = {
        harness.slug
        for harness in CANONICAL_HARNESSES
        if "distributed" in harness.topology_modes
    }
    # TensorFold's distributed support landed before its first catalog recipe;
    # the fixture refresh that adds one removes this allowance.
    assert distributed <= capable
    assert capable - distributed <= {"tensorfold"}
    assert {item.topology.node_count for item in recipes} >= {1, 2, 3, 4, 8}
    assert {item.interfaces[0].adapter for item in recipes} == {
        "openai",
        *get_args(RecipeJobInterface.model_fields["adapter"].annotation),
    }
    assert any(item.options for item in recipes)


@pytest.mark.parametrize("slug", sorted(RECIPES))
def test_catalog_recipe_compiles_to_the_committed_launch_payloads(slug: str) -> None:
    compiled = compile_recipe_payloads(RECIPES[slug], MODELS)

    expected = expected_payload_names(read_recipe(RECIPES[slug]))
    assert set(compiled) == expected
    for name, payload in compiled.items():
        validate_compiled_execution_plan(payload)
        assert payload == _committed(name), f"{name} drifted; {REFRESH}"


def test_no_launch_payload_fixture_is_stale() -> None:
    expected = {
        name
        for document in RECIPES.values()
        for name in expected_payload_names(read_recipe(document))
    }
    assert {path.name for path in PAYLOADS.glob("*.json")} == expected


def test_option_choices_change_the_compiled_launch() -> None:
    document = RECIPES["qwen3-8-flash-next-nvfp4-tonyd2wild-vllm-four"]
    recipe = read_recipe(document)
    compiled = compile_recipe_payloads(document, MODELS)
    default = compiled[f"{recipe.identity.slug}--default--rank0.json"]
    variants = [name for name in compiled if "--default--" not in name]
    assert variants
    for name in variants:
        runtime = compiled[name]["runtime"]
        assert (runtime["argv"], runtime["env"]) != (
            default["runtime"]["argv"],
            default["runtime"]["env"],
        )


def test_engine_environment_names_keep_their_case() -> None:
    payload = _committed("step-3-7-flash-nvfp4-r0b0tlab-vllm-dual--default--rank0.json")
    names = {item["name"] for item in payload["runtime"]["env"]}
    assert {"RAY_memory_usage_threshold", "RAY_memory_monitor_refresh_ms"} <= names


def _with_environment(name: str) -> dict[str, Any]:
    document = copy.deepcopy(RECIPES["step-3-7-flash-nvfp4-r0b0tlab-vllm-dual"])
    document["runtime"]["environment"].append({"name": name, "value": "1"})
    return document


@pytest.mark.parametrize(
    "name",
    ["lowercase_first", "HAS-DASH", "HAS=EQUALS", "HAS SPACE", "_LEADING", "PATH"],
)
def test_unsafe_or_platform_owned_environment_names_fail_closed(name: str) -> None:
    with pytest.raises((RecipeRuntimeSpecError, CompiledExecutionPlanError)):
        compile_recipe_payloads(_with_environment(name), MODELS)


def test_model_files_outside_the_models_root_fail_closed() -> None:
    document = copy.deepcopy(RECIPES["gemma-4-e2b-it-nvidia-vllm-single"])
    for selection in document["models"]:
        for selector in selection["files"]:
            selector["mount"]["target"] = "/model"
    with pytest.raises((RecipeRuntimeSpecError, CompiledExecutionPlanError)):
        compile_recipe_payloads(document, MODELS)


def test_a_role_without_model_files_fails_closed() -> None:
    document = copy.deepcopy(RECIPES["step-3-7-flash-nvfp4-r0b0tlab-vllm-dual"])
    for selection in document["models"]:
        for selector in selection["files"]:
            selector["roles"] = ["entrypoint"]
    with pytest.raises(RecipeRuntimeSpecError):
        compile_recipe_payloads(document, MODELS)


def test_distribution_is_refused_for_engines_without_a_distributed_harness() -> None:
    document = copy.deepcopy(RECIPES["gemma-4-e2b-it-nvidia-vllm-single"])
    document["runtime"]["engine"] = "llama-cpp"
    document["runtime"]["entrypoint"] = ["/opt/vonk/bin/llama-server"]
    dual = copy.deepcopy(RECIPES["step-3-7-flash-nvfp4-r0b0tlab-vllm-dual"])
    document["topology"] = dual["topology"]
    for selection in document["models"]:
        for selector in selection["files"]:
            selector["roles"] = ["entrypoint", "worker"]
    with pytest.raises(RecipeRuntimeSpecError):
        compile_recipe_payloads(document, MODELS)
