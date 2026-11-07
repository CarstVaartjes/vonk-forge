"""A route's LiteLLM ``model_info`` comes only from its recipe's API facts.

The shared vectors in ``fixtures/route_model_info/vectors.json`` pair a recipe
and its ``recipe-api`` document (the recipes repository's shipped examples; a
vector may patch the example's base block) with the mapping parameters the run carries
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
from vonk_agent_protocol import GatewayRouteState, RoutePublicationState, UnknownError
from vonk_control import route_model_info as module
from vonk_control.litellm import (
    LiteLlmConfig,
    LiteLlmModelInfo,
    LiteLlmPolicy,
    OpenAiRequestParameter,
    RouteState,
    render_config,
    render_empty_config,
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
from vonk_control.route_model_info import build_model_info
from vonk_control.route_runtime import (
    RECIPE_ROUTE_AUTHORITY_ID,
    AtomicRouteBundlePublisher,
    verify_active_route_bundle,
)
from vonk_forge_contracts import read_recipe, read_recipe_api

FIXTURES = Path(__file__).parent / "fixtures" / "route_model_info"
EXAMPLES = files("vonk_forge_contracts").joinpath("examples")
VECTORS = json.loads((FIXTURES / "vectors.json").read_text(encoding="utf-8"))


def _document(name: str) -> dict:
    return json.loads(EXAMPLES.joinpath(name).read_text(encoding="utf-8"))


def _api(vector: dict) -> dict:
    """The shipped recipe-api example, with the vector's patch over its base block."""
    document = _document(vector["api"])
    document["api"] = {**document["api"], **vector.get("api_patch", {})}
    return document


def _built(vector: dict) -> LiteLlmModelInfo:
    return build_model_info(
        read_recipe(_document(vector["recipe"])),
        read_recipe_api(_api(vector)),
        *split_option_choices(vector["parameters"]),
    )


@pytest.mark.parametrize("vector", VECTORS, ids=lambda vector: vector["name"])
def test_vector_builds_the_expected_model_info_and_entry(vector: dict) -> None:
    info = _built(vector)
    assert info.model_dump(mode="json", exclude_none=True) == vector["model_info"]

    rendered = render_config(
        RouteState({"synthetic": "http://10.0.0.2:8000/v1"}, "a" * 64),
        LiteLlmPolicy(
            {
                "synthetic": RouteAcceptedModelPolicy(
                    upstream_model="synthetic", model_info=info
                )
            }
        ),
    )
    [entry] = json.loads(rendered)["model_list"]
    assert entry["model_info"] == vector["model_info"]
    # The file the gateway reads is the file the Controller reads back.
    assert LiteLlmConfig.model_validate_json(rendered).model_list[0].model_info == info


def test_a_route_without_facts_renders_no_model_info() -> None:
    rendered = json.loads(
        render_config(
            RouteState({"synthetic": "http://10.0.0.2:8000/v1"}, "a" * 64),
            LiteLlmPolicy(
                {"synthetic": RouteAcceptedModelPolicy(upstream_model="synthetic")}
            ),
        )
    )
    assert "model_info" not in rendered["model_list"][0]
    assert rendered["router_settings"]["enable_pre_call_checks"] is False
    assert "rpm" not in rendered["model_list"][0]["litellm_params"]


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
    api = _document("recipe-api-options.json")
    api["api"]["default_output_tokens"] = {"argument": "max-new-tokens"}
    info = build_model_info(read_recipe(document), read_recipe_api(api), {}, {})
    assert info.vonk_default_output_tokens == 777


def test_the_reasoning_effort_name_is_standard_only_at_top_level() -> None:
    rich = next(vector for vector in VECTORS if vector["name"] == "options-rich")
    assert (
        OpenAiRequestParameter.REASONING_EFFORT in _built(rich).supported_openai_params
    )
    inside = {
        **rich,
        "api_patch": {
            **rich["api_patch"],
            "reasoning": {
                **rich["api_patch"]["reasoning"],
                "effort": {
                    **rich["api_patch"]["reasoning"]["effort"],
                    "via": "chat_template_kwargs",
                },
            },
        },
    }
    info = _built(inside)
    assert OpenAiRequestParameter.REASONING_EFFORT not in info.supported_openai_params
    assert info.vonk_reasoning_effort is not None
    assert info.vonk_reasoning_effort.via == "chat_template_kwargs"


def test_max_tokens_is_the_output_cap() -> None:
    fields = _built(VECTORS[0]).model_dump()
    with pytest.raises(ValidationError, match="max_tokens"):
        LiteLlmModelInfo.model_validate({**fields, "max_tokens": 5})


def _vector(name: str) -> dict:
    return next(vector for vector in VECTORS if vector["name"] == name)


def test_an_audio_route_needs_no_generation_settings() -> None:
    """The audio branch runs before the settings and modality reads."""
    vector = {**_vector("transcription"), "recipe": "recipe-job.json"}
    assert _document("recipe-job.json")["settings"]["kind"] not in (
        "generation",
        "decision",
    )
    info = _built(vector)
    assert info.mode == "audio_transcription"
    assert info.max_input_tokens is None and info.max_output_tokens is None
    assert info.supports_audio_input and not info.supports_audio_output


def test_the_audio_endpoint_is_the_mode_never_a_recipe_choice() -> None:
    assert _built(_vector("transcription")).supported_endpoints == [
        "/v1/audio/transcriptions"
    ]
    assert _built(_vector("speech")).supported_endpoints == ["/v1/audio/speech"]


def test_the_speech_health_check_voice_is_the_first_voice() -> None:
    assert _built(_vector("speech")).health_check_voice == "alloy"


@pytest.mark.parametrize(
    "change",
    [
        {"max_input_tokens": 10},
        {"max_output_tokens": 5, "max_tokens": 5},
        {"vonk_audio_speech": None},
        {"vonk_audio_transcription": {}},
        {"health_check_voice": "echo"},
        {"mode": "chat"},
    ],
    ids=["tokens", "output-tokens", "missing-block", "foreign-block", "voice", "mode"],
)
def test_the_mode_decides_which_audio_fields_apply(change: dict) -> None:
    fields = _built(_vector("speech")).model_dump()
    with pytest.raises(ValidationError):
        LiteLlmModelInfo.model_validate({**fields, **change})


def test_a_text_route_still_requires_its_input_limit() -> None:
    fields = _built(VECTORS[0]).model_dump()
    with pytest.raises(ValidationError, match="max_input_tokens"):
        LiteLlmModelInfo.model_validate({**fields, "max_input_tokens": None})


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


def _digest(document: dict) -> str:
    return hashlib.sha256(
        json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _identity_digest(identity: RouteRunIdentity) -> str:
    document = RouteIdentityDocument(runs=[identity], aliases={"synthetic": "x"})
    return _digest(document.model_dump(mode="json", exclude_none=True))


def test_a_changed_fact_is_a_new_bundle_digest() -> None:
    first, second = _built(VECTORS[0]), _built(VECTORS[1])
    assert (
        len({_identity_digest(_identity(item)) for item in (None, first, second)}) == 3
    )
    assert _identity_digest(_identity(first)) == _identity_digest(
        _identity(_built(VECTORS[0]))
    )


def test_a_route_without_facts_keeps_the_identity_it_had_before_the_field() -> None:
    """The earlier Controller's identity document, field for field."""
    earlier = {
        "schema_version": 1,
        "aliases": {"synthetic": "x"},
        "runs": [
            {
                "run_id": "run",
                "alias": "synthetic",
                "plan_digest": "c" * 64,
                "run_generation": 1,
                "upstream_model": "synthetic",
                "upstream_provider": "openai",
                "ranks": [],
            }
        ],
    }
    assert _identity_digest(_identity(None)) == _digest(earlier)


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
        {"model": RouteAcceptedModelPolicy(upstream_model="model", model_info=info)}
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
    assert publisher.served_model_info().routes == {"model": (run_id, info)}


def test_ingress_reads_the_index_entrys_facts_once_and_fails_open() -> None:
    from vonk_control.recipe_packages import _entry_api

    recipe = read_recipe(_document("recipe-options.json"))
    api = _document("recipe-api-options.json")
    assert _entry_api({"api": api}, recipe.identity) == read_recipe_api(api)
    assert _entry_api({}, recipe.identity) is None
    future = json.loads(json.dumps(api))
    future["api"]["mode"] = "embedding"  # a union member this contract lacks
    assert _entry_api({"api": future}, recipe.identity) is None
    foreign = json.loads(json.dumps(api))
    foreign["identity"]["slug"] = "someone-else"
    assert _entry_api({"api": foreign}, recipe.identity) is None


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


def _source_build_api() -> dict:
    api = _document("recipe-api-source-build.json")
    api["identity"]["slug"] = "qwen"
    return api


def _published_entries(root: Path) -> dict[str, dict]:
    verified = verify_active_route_bundle(root)
    assert not isinstance(verified, UnknownError) and verified.litellm is not None
    return {
        entry.model_name: entry.model_dump(mode="json", exclude_none=True)
        for entry in verified.litellm.model_list
    }


@pytest.mark.parametrize("stored", ["facts", "none", "corrupt"])
def test_the_published_route_carries_its_recipes_facts_and_never_depends_on_them(
    tmp_path: Path, stored: str
) -> None:
    from .test_recipe_routes import setup

    service, _publisher, applied, run_id = setup(tmp_path, ranks=1)
    _store_api(
        service,
        None if stored == "none" else _source_build_api(),
        corrupt=stored == "corrupt",
    )
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


def _choose_an_unknown_option(service) -> None:
    from sqlalchemy import update
    from vonk_control.models import ClusterMapping

    with service.sessions.begin() as session:
        session.execute(
            update(ClusterMapping).values(
                parameters={"option_choices": {"vision": "unknown"}}
            )
        )


def test_unusable_facts_are_reported_and_the_route_still_publishes(
    tmp_path: Path,
) -> None:
    from .test_recipe_routes import setup

    service, _publisher, applied, run_id = setup(tmp_path, ranks=1)
    _store_api(service, _source_build_api())
    _choose_an_unknown_option(service)
    service.publish_run(run_id)

    [entry] = json.loads(applied[-1])["model_list"]
    assert "model_info" not in entry
    assert "RecipeOptionError" in service._degraded[run_id]


def test_a_bug_in_the_builder_is_not_swallowed_as_missing_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from .test_recipe_routes import setup

    service, _publisher, _applied, run_id = setup(tmp_path, ranks=1)
    _store_api(service, _source_build_api())

    def broken(*_arguments: object) -> LiteLlmModelInfo:
        raise TypeError("a defect, not an absence")

    monkeypatch.setattr(module, "build_model_info", broken)
    with pytest.raises(TypeError, match="a defect"):
        service.publish_run(run_id)


def test_a_serving_route_keeps_its_facts_until_it_changes(tmp_path: Path) -> None:
    """New facts never rewrite a running route's gateway entry (no restart)."""
    from vonk_agent_protocol import RouteState as RunRouteState

    from .test_recipe_routes import (
        NOW,
        MutableClock,
        add_running_run,
        atomic_service,
        setup,
    )

    clock = MutableClock(NOW)
    base, _, _, first = setup(tmp_path / "database", clock=clock)
    service = atomic_service(base, tmp_path / "live", clock)
    service.publish_run(first)
    assert "model_info" not in _published_entries(tmp_path / "live")["qwen"]

    # Facts land for the revision the running route uses.
    _store_api(base, _source_build_api())
    second = add_running_run(
        base, first, alias="qwen-next", route_state=RunRouteState.PENDING, identity=3
    )
    service.publish_run(second)

    entries = _published_entries(tmp_path / "live")
    assert "model_info" not in entries["qwen"]
    assert entries["qwen-next"]["model_info"]["mode"] == "chat"


def test_the_bundle_is_never_read_inside_the_publication_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Our own files are verified before the owner lock, not under row locks."""
    from vonk_control.recipe_routes import publisher as publisher_module
    from vonk_control.recipe_routes.shared import _SQLITE_ROUTE_PUBLICATION_LOCK

    from .test_recipe_routes import NOW, MutableClock, atomic_service, setup

    clock = MutableClock(NOW)
    base, _, _, run_id = setup(tmp_path / "database", clock=clock)
    service = atomic_service(base, tmp_path / "live", clock)
    real = publisher_module.verify_active_route_bundle
    held: list[bool] = []

    def observed(root: Path):
        held.append(_SQLITE_ROUTE_PUBLICATION_LOCK.locked())
        return real(root)

    monkeypatch.setattr(publisher_module, "verify_active_route_bundle", observed)
    service.publish_run(run_id)
    service.maintain()
    service.withdraw_run(run_id)
    assert held and not any(held)


def test_a_recovery_restart_of_the_same_run_applies_the_current_facts(
    tmp_path: Path,
) -> None:
    """A restarted run is a route change: its facts are read again, not frozen."""
    from sqlalchemy import update
    from vonk_agent_protocol import RouteState as RunRouteState
    from vonk_control.models import RecipeRun, RunNode

    from .test_recipe_routes import NOW, MutableClock, atomic_service, setup

    clock = MutableClock(NOW)
    base, _, _, run_id = setup(tmp_path / "database", clock=clock)
    service = atomic_service(base, tmp_path / "live", clock)
    service.publish_run(run_id)
    assert "model_info" not in _published_entries(tmp_path / "live")["qwen"]

    # Facts land while the route serves: nothing moves.
    _store_api(base, _source_build_api())
    service.maintain()
    assert "model_info" not in _published_entries(tmp_path / "live")["qwen"]

    # Recovery restarts the same run: a new generation, route pending again.
    with base.sessions.begin() as session:
        session.execute(
            update(RecipeRun)
            .where(RecipeRun.id == run_id)
            .values(
                run_generation=RecipeRun.run_generation + 1,
                route_state=RunRouteState.PENDING,
            )
        )
        # The ranks report the new generation.
        session.execute(
            update(RunNode)
            .where(RunNode.run_id == run_id)
            .values(observed_run_generation=RunNode.observed_run_generation + 1)
        )
    service.publish_run(run_id)
    assert _published_entries(tmp_path / "live")["qwen"]["model_info"]["mode"] == "chat"


def _endpoint_read_fails(base, root: Path) -> bool:
    """Whether the durable endpoint projection cannot verify the live bundle."""
    from vonk_control.operation_api.durable import _DurableOperationProjection

    projection = _DurableOperationProjection(
        base.sessions,
        root,
        clock=lambda: datetime.now(UTC),
        stale_after_seconds=60,
        cursors=None,  # type: ignore[arg-type]
    )
    with base.sessions() as session:
        snapshot = projection._publication_snapshot(session)
    try:
        projection._verified_routes(snapshot)
    except RuntimeError:
        return True
    return False


def test_an_earlier_bundle_is_republished_once_by_maintenance(
    tmp_path: Path,
) -> None:
    """An earlier ``litellm.json`` (rpm/tpm, pre-call checks on) no longer parses.

    The one contract has no reader for it, so the active bundle is Unknown and
    the next maintenance pass republishes the whole file under the current
    policy: no count cap, pre-call checks off. That is the one LiteLLM restart
    the upgrade causes; the next pass finds the bundle current and does nothing.
    """
    from .test_recipe_routes import NOW, MutableClock, atomic_service, setup

    clock = MutableClock(NOW)
    base, _, _, run_id = setup(tmp_path / "database", clock=clock)
    service = atomic_service(base, tmp_path / "live", clock)
    service.publish_run(run_id)
    live = tmp_path / "live"
    published = verify_active_route_bundle(live)
    assert not isinstance(published, UnknownError)
    assert published.routes is not None and published.litellm is not None

    # The same route as the earlier Controller wrote it: a count cap per
    # deployment and pre-call checks on.
    earlier = json.loads(render_empty_config())
    earlier["model_list"] = [
        {
            **entry.model_dump(mode="json", exclude_none=True),
            "litellm_params": {
                **entry.litellm_params.model_dump(mode="json", exclude_none=True),
                "rpm": 60,
                "tpm": 1_000_000,
            },
        }
        for entry in published.litellm.model_list
    ]
    earlier["router_settings"]["enable_pre_call_checks"] = True
    earlier_bytes = (
        json.dumps(earlier, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    runtime = AtomicRouteBundlePublisher(live)
    # The current validator refuses these bytes; the earlier Controller's did not.
    runtime._validate_litellm = lambda _bytes: True  # type: ignore[method-assign]
    with runtime._locked() as uncertainty:
        assert uncertainty is None
        marker = runtime._activate(
            generation=published.marker.generation + 1,
            state=GatewayRouteState.PUBLISHED,
            authority_id=RECIPE_ROUTE_AUTHORITY_ID,
            plan_digest=published.marker.plan_digest,
            evidence_set_digest=published.marker.plan_digest,
            routes=published.routes.model_copy(
                update={"generation": published.marker.generation + 1}
            )
            .model_dump_json()
            .encode(),
            litellm=earlier_bytes,
        )
    assert not isinstance(marker, UnknownError)
    # The database records exactly what the earlier Controller recorded for
    # that publication: its generation, marker and file digests.
    with service.sessions.begin() as session:
        service.projection_in_session(
            session,
            SimpleNamespace(activation_marker=marker),  # type: ignore[arg-type]
            state=RoutePublicationState.COMPLETED,
        )
    assert isinstance(verify_active_route_bundle(live), UnknownError)
    assert service._publisher.active_marker_digest() == marker.digest
    served = service._publisher.served_model_info()
    assert served.routes == {} and served.unreadable
    # Every endpoint read fails until the bundle is republished.
    assert _endpoint_read_fails(base, live)

    # One maintenance pass republishes under the current policy, though the
    # candidate digest and the durable marker are unchanged.
    assert service.maintain() is True
    entries = _published_entries(live)
    assert set(entries) == {"qwen"}
    assert "rpm" not in entries["qwen"]["litellm_params"]
    assert "tpm" not in entries["qwen"]["litellm_params"]
    after = verify_active_route_bundle(live)
    assert not isinstance(after, UnknownError) and after.litellm is not None
    assert after.litellm.router_settings.enable_pre_call_checks is False
    assert not _endpoint_read_fails(base, live)

    # Healed: the next pass has nothing to publish and no byte moves.
    assert service.maintain() is False
    settled = verify_active_route_bundle(live)
    assert not isinstance(settled, UnknownError)
    assert settled.marker.litellm_sha256 == after.marker.litellm_sha256
