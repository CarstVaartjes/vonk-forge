"""Catalog dry run: every recipe passes the Controller's offline pipeline.

Each invariant is one test over every recipe of the canonical library (see
``catalog_dry_run``), so a recipe or platform defect that would otherwise
surface on a Spark fails here with the recipe's name.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from vonk_control.litellm import LiteLlmPolicy, RouteState, render_config
from vonk_control.prebuilt_images import (
    _bundle_relative,
    _library_recipe,
    executable_build_key,
    package_build_intent,
)
from vonk_control.revision_images import revision_source_bundle
from vonk_forge_contracts import read_recipe
from vonk_forge_contracts.recipe import RecipeOpenAIInterface

from .catalog_dry_run import (
    CatalogDryRun,
    catalog_index,
    import_catalog,
    indexed_recipes,
)
from .recipe_library_source import recipe_library_root

ROOT = recipe_library_root()
INDEX = catalog_index(ROOT)
RECIPES = indexed_recipes(INDEX)

# Known recipe defects (strict xfails): exact documents of the pinned library
# whose gateway upstream model the engine does not serve. Keyed by content
# digest, so a fixed document is another revision and simply passes; prune
# entries the pinned library no longer contains when re-pinning it.
UNSERVED_UPSTREAM_MODEL = {
    # Served no alias or the repository id; fixed by vonk-forge-recipes #1222.
    "817e3bf705751767bcc421bfc32c223163613661475adbaa81b6bca220811c7e": "#1222",  # deepseek-v4-1-flash-exl3-sfxnz-vllm-dual
    "0ea44445994f9bd6c2a9682c6eb9c89f98809dd1008e41529dca3d7edf9712ed": "#1222",  # deepseek-v4-1-flash-r0b0tlab-sglang-four
    "44252cf5247f766f6fd75bf8d17dd66017f213cf5de565ce5b748963e76853c6": "#1222",  # nex-n2-mini-nvfp4-r0b0tlab-vllm-single
    "f9ec3823a4ca44174b2e18025dd06758dc7107f8cb1e5443dcc7a54dfbeb9952": "#1222",  # qwen3-8-flash-next-nvfp4-sfxnz-vllm-dual
    "b0e3601671a2be9537349dbc79db8378f32672812b66f4a51ed273f30f6aa445": "#1222",  # vibethinker-3b-nvfp4-r0b0tlab-vllm-single
    # Upstream model '/models/Step-3.7-Flash-NVFP4' fails the gateway's model
    # name pattern; still open on recipes main.
    "7e75e6235b7487eefa9256d3d880fe8c1b846802ba83b011eaf75f64d0c4b293": "open",  # step-3-7-flash-nvfp4-r0b0tlab-vllm-dual
}
# Engines whose OpenAI server answers only the names it serves (404 otherwise).
# The gateway forwards the upstream model unchanged, so it must be one of them.
NAME_ROUTING_ENGINES = frozenset({"vllm", "sglang"})


def _params(
    xfail: Callable[[str], str | None] = lambda _slug: None,
    *,
    only: Callable[[str], bool] = lambda _slug: True,
) -> list[Any]:
    """The recipes, each with its known failure (if any) as a strict xfail."""

    params = []
    for slug in filter(only, sorted(RECIPES)):
        reason = xfail(slug)
        marks = () if reason is None else pytest.mark.xfail(strict=True, reason=reason)
        params.append(pytest.param(slug, marks=marks, id=slug))
    return params


def _unserved_upstream_model(slug: str) -> str | None:
    fix = UNSERVED_UPSTREAM_MODEL.get(_digest(slug))
    if fix is None:
        return None
    return f"the engine does not serve the gateway's upstream model ({fix})"


def _document(slug: str) -> dict[str, Any]:
    return RECIPES[slug]["document"]


def _digest(slug: str) -> str:
    return str(RECIPES[slug]["content_sha256"])


def _upstream_model(slug: str) -> str | None:
    """The model name the gateway forwards, as run admission persists it."""

    return next(
        (
            interface.model_aliases[0]
            for interface in read_recipe(_document(slug)).interfaces
            if isinstance(interface, RecipeOpenAIInterface) and interface.model_aliases
        ),
        None,
    )


@pytest.fixture(scope="module")
def dry_run(tmp_path_factory: pytest.TempPathFactory) -> Iterator[CatalogDryRun]:
    yield from import_catalog(ROOT, tmp_path_factory.mktemp("catalog-dry-run"))


@pytest.mark.parametrize("slug", _params())
def test_build_identity_is_the_packaged_source_bundle(
    slug: str, dry_run: CatalogDryRun
) -> None:
    """Builds are selected and prebuilt by the bundle the package carries."""

    item, bundle = _library_recipe(ROOT, INDEX, RECIPES[slug])
    assert item.source_bundle_sha256 == bundle.sha256
    assert revision_source_bundle(dry_run.revision(_digest(slug))) == bundle.sha256
    build = read_recipe(item.document).execution.build
    intent = package_build_intent(
        item.document,
        source_bundle_sha256=bundle.sha256,
        dockerfile_payload=dict(bundle.files)[
            _bundle_relative(build.dockerfile, build.context.path)
        ],
    )
    assert intent["source_bundle_sha256"] == bundle.sha256
    executable_build_key(intent)


@pytest.mark.parametrize("slug", _params())
def test_every_spark_compiles_against_the_cache_manifest(
    slug: str, dry_run: CatalogDryRun
) -> None:
    plans = dry_run.plans(_digest(slug))
    manifest = dry_run.manifest(_digest(slug))
    assert len(plans) == read_recipe(_document(slug)).topology.world_size
    for plan in plans.values():
        assert plan.identity.model_artifact_set_sha256 == manifest.digest


@pytest.mark.parametrize("slug", _params())
def test_plan_and_transfer_bytes_agree(slug: str, dry_run: CatalogDryRun) -> None:
    manifest = dry_run.manifest(_digest(slug))
    transfer = dry_run.transfer(_digest(slug))
    assert transfer.artifact_set_sha256 == manifest.digest
    assert transfer.total_bytes == manifest.expected_bytes
    assert {item.name for item in transfer.objects} == {
        spec.path for spec in manifest.artifacts
    }


@pytest.mark.parametrize("slug", _params())
def test_every_spark_receives_a_valid_assignment(
    slug: str, dry_run: CatalogDryRun
) -> None:
    manifest = dry_run.manifest(_digest(slug))
    assignments = dry_run.assignments(_digest(slug))
    assert len(assignments) == read_recipe(_document(slug)).topology.world_size
    for assignment in assignments:
        assert {item.sha256 for item in assignment.objects} == {
            spec.sha256 for spec in manifest.artifacts
        }


@pytest.mark.parametrize(
    "slug",
    _params(_unserved_upstream_model, only=lambda slug: bool(_upstream_model(slug))),
)
def test_gateway_forwards_a_model_name_the_engine_serves(
    slug: str, dry_run: CatalogDryRun
) -> None:
    upstream = _upstream_model(slug)
    assert upstream is not None
    alias = "catalog-dry-run"
    config = json.loads(
        render_config(
            RouteState(aliases={alias: "http://100.100.20.30:8000/v1"}, digest="0"),
            LiteLlmPolicy(
                models={
                    alias: {
                        "requests_per_minute": 60,
                        "tokens_per_minute": 1_000_000,
                        "upstream_model": upstream,
                    }
                }
            ),
        )
    )
    [entry] = config["model_list"]
    assert entry["litellm_params"]["model"] == f"openai/{upstream}"
    recipe = read_recipe(_document(slug))
    if recipe.runtime.engine not in NAME_ROUTING_ENGINES:
        return
    # The gateway routes to the one rank mapped to the entrypoint role.
    [entrypoint] = [
        plan
        for node_id, plan in dry_run.plans(_digest(slug)).items()
        if dry_run.roles(_digest(slug))[node_id] == "entrypoint"
    ]
    if "--profile" in entrypoint.runtime.argv:
        return  # r0b0tlab's launcher compiles its own argv, served name included
    assert upstream in _served_names(entrypoint.runtime.argv)


def _served_names(argv: list[str]) -> set[str]:
    names: set[str] = set()
    for index, token in enumerate(argv):
        if token.startswith("--served-model-name="):
            names.add(token.split("=", 1)[1])
        elif token == "--served-model-name":
            for value in argv[index + 1 :]:
                if value.startswith("--"):
                    break
                names.add(value)
    return names
