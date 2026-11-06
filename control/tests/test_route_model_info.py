"""A route's LiteLLM ``model_info`` comes only from its recipe's API facts.

The shared vectors in ``fixtures/route_model_info/vectors.json`` pair a recipe
and its ``recipe-api`` document (the recipes repository's shipped examples, plus
one rich chat document kept here) with the mapping parameters the run carries
and the exact ``model_info`` JSON the gateway must publish. The rendered
LiteLLM entry carries that JSON verbatim, and ``/v1/model/info`` returns an
entry's ``model_info`` verbatim, custom keys included.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError
from vonk_agent_protocol import GatewayRouteState, UnknownError
from vonk_control import route_model_info as module
from vonk_control.catalog_revision_contract import CatalogRevisionContractError
from vonk_control.litellm import (
    LiteLlmConfig,
    LiteLlmModelInfo,
    LiteLlmPolicy,
    RouteState,
    render_config,
)
from vonk_control.presence import ManagementAddressPolicy
from vonk_control.recipe_routes import AtomicRecipeRoutePublisher
from vonk_control.recipe_routes.shared import _RecipeEndpoint
from vonk_control.recipe_runtime_specs import split_option_choices
from vonk_control.route_bundle_contract import (
    RouteAcceptedModelPolicy,
    RouteBundleDocument,
    RouteIdentityDocument,
    RouteRunIdentity,
)
from vonk_control.route_model_info import build_model_info, route_model_info
from vonk_control.route_runtime import (
    RECIPE_ROUTE_AUTHORITY_ID,
    AtomicRouteBundlePublisher,
)
from vonk_forge_contracts import read_recipe, read_recipe_api

FIXTURES = Path(__file__).parent / "fixtures" / "route_model_info"
EXAMPLES = files("vonk_forge_contracts").joinpath("examples")
VECTORS = json.loads((FIXTURES / "vectors.json").read_text(encoding="utf-8"))


def _document(name: str) -> dict:
    local = FIXTURES / name
    text = (
        local.read_text(encoding="utf-8")
        if local.exists()
        else EXAMPLES.joinpath(name).read_text(encoding="utf-8")
    )
    return json.loads(text)


def _built(vector: dict) -> LiteLlmModelInfo:
    return build_model_info(
        read_recipe(_document(vector["recipe"])),
        read_recipe_api(_document(vector["api"])),
        *split_option_choices(vector["parameters"]),
    )


@pytest.mark.parametrize("vector", VECTORS, ids=lambda vector: vector["name"])
def test_vector_builds_the_expected_model_info_and_entry(vector: dict) -> None:
    info = _built(vector)
    assert info.model_dump(mode="json", exclude_none=True) == vector["model_info"]

    rendered = json.loads(
        render_config(
            RouteState({"synthetic": "http://10.0.0.2:8000/v1"}, "a" * 64),
            LiteLlmPolicy(
                {
                    "synthetic": RouteAcceptedModelPolicy(
                        requests_per_minute=60,
                        tokens_per_minute=1_000_000,
                        upstream_model="synthetic",
                        model_info=info,
                    )
                }
            ),
        )
    )
    [entry] = rendered["model_list"]
    assert entry["model_info"] == vector["model_info"]
    # The file the gateway reads is the file the Controller reads back.
    assert LiteLlmConfig.model_validate(rendered).model_list[0].model_info == info


def test_a_route_without_facts_renders_no_model_info() -> None:
    rendered = json.loads(
        render_config(
            RouteState({"synthetic": "http://10.0.0.2:8000/v1"}, "a" * 64),
            LiteLlmPolicy(
                {
                    "synthetic": RouteAcceptedModelPolicy(
                        requests_per_minute=60,
                        tokens_per_minute=1_000_000,
                        upstream_model="synthetic",
                    )
                }
            ),
        )
    )
    assert "model_info" not in rendered["model_list"][0]


def test_shipped_option_vectors_follow_the_chosen_block() -> None:
    shipped = json.loads(
        EXAMPLES.joinpath("api-vectors", "option-choices.json").read_text("utf-8")
    )
    recipe = read_recipe(_document(shipped["recipe"]))
    api = read_recipe_api(_document(shipped["api"]))
    for vector in shipped["vectors"]:
        info = build_model_info(recipe, api, vector["choices"], {})
        assert info.supports_vision is vector["api"]["input_modalities"]["image"]
        assert info.supported_endpoints == vector["api"]["endpoints"]


def test_the_default_output_cap_can_name_a_runtime_argument() -> None:
    document = _document("recipe-options.json")
    document["runtime"]["arguments"].append({"name": "max-new-tokens", "value": 777})
    api = _document("rich-chat.api.json")
    api["api"]["default_output_tokens"] = {"argument": "max-new-tokens"}
    info = build_model_info(read_recipe(document), read_recipe_api(api), {}, {})
    assert info.vonk_default_output_tokens == 777


def test_max_tokens_is_the_output_cap() -> None:
    fields = _built(VECTORS[0]).model_dump()
    with pytest.raises(ValidationError, match="max_tokens"):
        LiteLlmModelInfo.model_validate({**fields, "max_tokens": 5})


class _Session:
    """Just enough of a session for the route lookup."""

    def __init__(self, installation, revision) -> None:
        self._rows = {
            "RecipeInstallation": installation,
            "CatalogDocumentRevision": revision,
        }

    def get(self, model, _key):
        return self._rows[model.__name__]


_RUN = SimpleNamespace(id="run", installation_id="installation")


def _facts_session() -> _Session:
    return _Session(
        SimpleNamespace(recipe_revision_id="revision"),
        SimpleNamespace(document=_document("recipe-options.json")),
    )


@pytest.mark.parametrize(
    "case", ["no installation", "no revision", "unreadable projection", "no facts"]
)
def test_missing_or_unreadable_facts_publish_no_model_info(
    monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    """Fail open: the route stays up and simply has no model_info."""
    session = _facts_session()
    if case == "no installation":
        session = _Session(None, None)
    elif case == "no revision":
        session = _Session(SimpleNamespace(recipe_revision_id="x"), None)
    elif case == "unreadable projection":

        def unreadable(_revision):
            raise CatalogRevisionContractError("catalog projection is invalid")

        monkeypatch.setattr(module, "read_recipe_projection", unreadable)
    else:
        monkeypatch.setattr(
            module,
            "read_recipe_projection",
            lambda _revision: SimpleNamespace(api=None),
        )
    assert route_model_info(session, _RUN, None) is None  # type: ignore[arg-type]


def test_an_option_the_recipe_no_longer_has_publishes_no_model_info(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = read_recipe_api(_document("rich-chat.api.json"))
    monkeypatch.setattr(
        module, "read_recipe_projection", lambda _revision: SimpleNamespace(api=api)
    )
    mapping = SimpleNamespace(parameters={"option_choices": {"vision": "retired"}})
    assert route_model_info(_facts_session(), _RUN, mapping) is None  # type: ignore[arg-type]
    mapping = SimpleNamespace(parameters={"option_choices": {"vision": "images"}})
    info = route_model_info(_facts_session(), _RUN, mapping)  # type: ignore[arg-type]
    assert info is not None and info.supports_vision


def _identity(info: LiteLlmModelInfo | None) -> RouteRunIdentity:
    return RouteRunIdentity(
        run_id="run",
        alias="synthetic",
        plan_digest="c" * 64,
        run_generation=1,
        upstream_model="synthetic",
        model_info=info,
        ranks=[],
    )


def _digest(identity: RouteRunIdentity) -> str:
    document = RouteIdentityDocument(runs=[identity], aliases={"synthetic": "x"})
    return hashlib.sha256(
        json.dumps(
            document.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()


def test_a_changed_fact_is_a_new_bundle_digest() -> None:
    first, second = _built(VECTORS[0]), _built(VECTORS[1])
    assert len({_digest(_identity(item)) for item in (None, first, second)}) == 3
    assert _digest(_identity(first)) == _digest(_identity(_built(VECTORS[0])))


def test_a_retained_route_keeps_its_facts(tmp_path: Path) -> None:
    """The accepted bundle's litellm.json is where a retained route reads them."""
    info = _built(VECTORS[0])
    run_id = str(uuid4())
    endpoint = _RecipeEndpoint(
        "spk_" + "a" * 32,
        "10.0.0.2",
        8000,
        datetime.now(UTC),
        f"recipe:{run_id}:rank:0",
    )
    state = RouteState({"model": endpoint.api_base}, "a" * 64)
    policy = LiteLlmPolicy(
        {
            "model": RouteAcceptedModelPolicy(
                requests_per_minute=60,
                tokens_per_minute=1_000_000,
                upstream_model="model",
                model_info=info,
            )
        }
    )
    runtime = AtomicRouteBundlePublisher(tmp_path)
    publisher = AtomicRecipeRoutePublisher(runtime)
    document = RouteBundleDocument(
        generation=1,
        routes={"model": endpoint.route_document()},
        schema_version=2,
        state=GatewayRouteState.PUBLISHED,
    )
    with runtime._locked() as uncertainty:
        assert uncertainty is None
        marker = runtime._activate(
            generation=1,
            state=GatewayRouteState.PUBLISHED,
            authority_id=RECIPE_ROUTE_AUTHORITY_ID,
            plan_digest=state.digest,
            evidence_set_digest=state.digest,
            routes=document.model_dump_json().encode(),
            litellm=render_config(state, policy),
        )
    assert not isinstance(marker, UnknownError)
    accepted = publisher.accepted_run(
        run_id, ManagementAddressPolicy.parse("10.0.0.0/24")
    )
    assert accepted is not None
    assert accepted.policy.models["model"].model_info == info


def _store_api(base, api_document: dict | None, *, corrupt: bool = False) -> None:
    """Record the facts beside the run's revision the way the catalog sync does."""
    from sqlalchemy import select, update
    from vonk_control.catalog_revision_contract import (
        read_catalog_document,
        rederive_projection,
        write_catalog_projection,
    )
    from vonk_control.models import CatalogDocumentRevision

    with base.sessions.begin() as session:
        revision = session.scalars(
            select(CatalogDocumentRevision).where(
                CatalogDocumentRevision.kind == "recipe"
            )
        ).one()
        projected = rederive_projection({}, read_catalog_document(revision))
        if api_document is not None:
            projected = projected.model_copy(
                update={"api": read_recipe_api(api_document)}
            )
        stored = write_catalog_projection(projected)
        if corrupt:
            stored["api"] = {"kind": "recipe-api", "api": {"mode": "from-the-future"}}
        session.execute(
            update(CatalogDocumentRevision)
            .where(CatalogDocumentRevision.id == revision.id)
            .values(projected=stored)
        )


@pytest.mark.parametrize("stored", ["facts", "none", "corrupt"])
def test_the_published_route_carries_its_recipes_facts_and_never_depends_on_them(
    tmp_path: Path, stored: str
) -> None:
    from .test_recipe_routes import setup

    service, _publisher, applied, run_id = setup(tmp_path, ranks=1)
    api = _document("recipe-api-source-build.json")
    api["identity"]["slug"] = "qwen"
    _store_api(service, None if stored == "none" else api, corrupt=stored == "corrupt")
    service.publish_run(run_id)

    [entry] = json.loads(applied[-1])["model_list"]
    assert entry["model_name"] == "qwen"
    if stored == "facts":
        expected = build_model_info(
            read_recipe(_document("recipe-source-build.json")),
            read_recipe_api(_document("recipe-api-source-build.json")),
            {},
            {},
        )
        assert entry["model_info"] == expected.model_dump(
            mode="json", exclude_none=True
        )
    else:
        assert "model_info" not in entry


def test_ingress_reads_the_index_entrys_facts_once_and_fails_open() -> None:
    from vonk_control.recipe_packages import _entry_api

    recipe = read_recipe(_document("recipe-options.json"))
    api = _document("rich-chat.api.json")
    assert _entry_api({"api": api}, recipe.identity) == read_recipe_api(api)
    assert _entry_api({}, recipe.identity) is None
    future = json.loads(json.dumps(api))
    future["api"]["mode"] = "embedding"  # a union member this contract lacks
    assert _entry_api({"api": future}, recipe.identity) is None
    foreign = json.loads(json.dumps(api))
    foreign["identity"]["slug"] = "someone-else"
    assert _entry_api({"api": foreign}, recipe.identity) is None
