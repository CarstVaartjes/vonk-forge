from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping
from importlib import resources


def _recipe(
    slug: str,
    *,
    nodes: int = 1,
    recipe_id: str | None = None,
    revision_id: str | None = None,
) -> dict[str, object]:
    """Return a complete canonical recipe document for Library fixtures."""
    document = json.loads(
        resources.files("vonk_forge_contracts")
        .joinpath("examples", "recipe-source-build.json")
        .read_text(encoding="utf-8")
    )
    document["identity"]["publisher"] = "vonk"  # type: ignore[index]
    document["identity"]["slug"] = slug  # type: ignore[index]
    topology = document["topology"]
    topology["node_count"] = nodes  # type: ignore[index]
    topology["name"] = "solo" if nodes == 1 else "dual"  # type: ignore[index]
    topology["mode"] = "single" if nodes == 1 else "distributed"  # type: ignore[index]
    roles = topology["roles"]  # type: ignore[index]
    roles[0]["count"] = nodes  # type: ignore[index]
    if recipe_id is not None:
        document["__recipe_id"] = recipe_id
    if revision_id is not None:
        document["__revision_id"] = revision_id
    return document


def _library_detail(
    recipe: Mapping[str, object],
    *,
    recipe_id: str | None = None,
    revision_id: str | None = None,
    **overrides: object,
) -> dict[str, object]:
    """Build the current generated Library list/detail transport shape."""
    from cluster_profiles.generated_control.models.recipe_definition import (
        RecipeDefinition,
    )

    definition = RecipeDefinition.from_dict(recipe)
    canonical = definition.to_dict()
    digest = hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    identity = definition.identity
    rid = recipe_id or str(
        recipe.get("__recipe_id")
        or uuid.uuid5(uuid.NAMESPACE_URL, f"vonk:{identity.publisher}/{identity.slug}")
    )
    rev = revision_id or str(
        recipe.get("__revision_id")
        or uuid.uuid5(uuid.NAMESPACE_URL, f"vonk:{rid}:revision")
    )
    model_documents = []
    for selection in canonical["models"]:
        model = json.loads(
            resources.files("vonk_forge_contracts")
            .joinpath("examples", "model-definition.json")
            .read_text(encoding="utf-8")
        )
        selected = selection["model"]
        model["identity"]["publisher"] = selected["publisher"]
        model["identity"]["slug"] = selected["slug"]
        model["identity"]["model"]["publisher"] = selected["publisher"]
        model["identity"]["model"]["slug"] = selected["slug"]
        model_documents.append({"selection": selection, "model_document": model})
    summary = {
        "capabilities": [],
        "content_sha256": digest,
        "description": identity.slug,
        "installation_returned_count": 0,
        "installation_total_count": 0,
        "installations": [],
        "installations_truncated": False,
        "publisher": identity.publisher,
        "reasons": [],
        "recipe_document": canonical,
        "recipe_id": rid,
        "recipe_revision_id": rev,
        "run_returned_count": 0,
        "run_total_count": 0,
        "runs": [],
        "runs_truncated": False,
        "slug": identity.slug,
        "title": identity.slug,
        "topology_name": definition.topology.name,
    }
    detail = {
        "schema_version": 2,
        "generated_at": "2026-09-07T00:00:00Z",
        "recipe": {
            "content_sha256": digest,
            "description": identity.slug,
            "publisher": identity.publisher,
            "recipe_id": rid,
            "recipe_revision_id": rev,
            "slug": identity.slug,
            "title": identity.slug,
        },
        "definition": canonical,
        "topology": canonical["topology"],
        "operational_state": {
            "builds": [],
            "mappings": [],
            "installations": [],
            "runs": [],
        },
        "placement": [],
        "reasons": [],
        "model_documents": model_documents,
    }
    detail.update(overrides)
    return {"summary": summary, "detail": detail}


def _require_object(value: object) -> Mapping[str, object]:
    """Return *value* as a JSON object, or the TypeError a subscript would raise."""
    if not isinstance(value, Mapping):
        raise TypeError(f"{type(value).__name__!r} object is not subscriptable")
    return value


def _recipe_digest(slug: str = "tiny") -> str:
    detail = _require_object(_library_detail(_recipe(slug))["detail"])
    return str(_require_object(detail["recipe"])["content_sha256"])


def _recipe_projection(
    selector: str,
    title: str,
    *,
    recipe_id: str | None = None,
    model_selector: str | None = None,
):
    """A selector fixture uses the same canonical projection as the owner."""
    from datetime import UTC, datetime

    from cluster_profiles.cli_states_generated import UNKNOWN
    from cluster_profiles.generated_control.models.library_local_state import (
        LibraryLocalState,
    )
    from cluster_profiles.generated_control.models.library_recipe_identity import (
        LibraryRecipeIdentity,
    )
    from cluster_profiles.generated_control.models.library_recipe_projection import (
        LibraryRecipeProjection,
    )
    from cluster_profiles.generated_control.models.library_resource_projection import (
        LibraryResourceProjection,
    )
    from cluster_profiles.generated_control.models.recipe_definition import (
        RecipeDefinition,
    )

    publisher, slug = selector.split("/")
    document = RecipeDefinition.from_dict(_recipe(slug))
    if model_selector is not None:
        model_publisher, model_slug = model_selector.split("/")
        document.models[0].model.publisher = model_publisher
        document.models[0].model.slug = model_slug
    models = [f"{item.model.publisher}/{item.model.slug}" for item in document.models]
    return LibraryRecipeProjection(
        document=document,
        engine="vllm",
        identity=LibraryRecipeIdentity(
            content_sha256="a" * 64,
            description="fixture",
            publisher=publisher,
            recipe_id=recipe_id or str(uuid.uuid5(uuid.NAMESPACE_URL, selector)),
            recipe_revision_id=str(
                uuid.uuid5(uuid.NAMESPACE_URL, selector + ":revision")
            ),
            slug=slug,
            title=title,
        ),
        local=LibraryLocalState(controller=UNKNOWN),
        model_selectors=models,
        node_count=1,
        resources=LibraryResourceProjection(),
        selector=selector,
        updated_at=datetime(2026, 10, 9, tzinfo=UTC),
        usage=[],
    ).to_dict()
